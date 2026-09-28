from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Update
from sqlalchemy import func, select
from app import admin, handlers
from app.config import settings
from app.database import Application, Event, Outbox, Reminder, User
from app.google_sheets import CITY_FIELDS, OFFER_FIELDS, sync, write_sheets
from app.middleware import TransactionMiddleware
from app.services import plan


def message_update(uid, update_id, text):
    return Update.model_validate({'update_id': update_id, 'message': {'message_id': update_id, 'date': datetime.now(timezone.utc), 'chat': {'id': uid, 'type': 'private'}, 'from': {'id': uid, 'is_bot': False, 'first_name': 'Test'}, 'text': text}})


def callback_update(uid, update_id, data):
    return Update.model_validate({'update_id': update_id, 'callback_query': {'id': str(update_id), 'from': {'id': uid, 'is_bot': False, 'first_name': 'Test'}, 'chat_instance': 'test', 'data': data}})


@pytest.fixture
async def dispatch(sessions, monkeypatch):
    monkeypatch.setattr('app.middleware.Session', sessions)
    bot = Bot('123456:TEST_TOKEN_NOT_REAL')
    bot.session = AsyncMock(return_value=True)
    dp = Dispatcher()
    dp.update.outer_middleware(TransactionMiddleware())
    r = Router()
    from aiogram.filters import CommandStart
    from aiogram import F
    r.message.register(handlers.start, CommandStart())
    r.callback_query.register(admin.admin_callback, F.data.startswith('adm:'))
    r.callback_query.register(handlers.callback)
    r.message.register(admin.admin_text)
    dp.include_router(r)
    yield lambda update: dp.feed_update(bot, update)
    await bot.session.close()


async def test_end_to_end_buttons_and_admin_security(sessions, dispatch):
    await dispatch(message_update(20, 1, '/start vk_moscow_01'))
    await dispatch(callback_update(20, 2, 'offer:first:conditions'))
    await dispatch(callback_update(20, 3, 'apply:first'))
    await dispatch(message_update(20, 4, 'Иван'))
    await dispatch(message_update(20, 5, '21'))
    async with sessions() as s:
        app = await s.scalar(select(Application).where(Application.user_id == 20))
        aid = app.id
    await dispatch(callback_update(20, 6, f'answer:{aid}:city:moscow'))
    await dispatch(message_update(20, 7, 'Центр'))
    await dispatch(message_update(20, 8, '+79991234567'))
    await dispatch(callback_update(20, 9, f'answer:{aid}:readiness:0'))
    await dispatch(callback_update(20, 10, f'answer:{aid}:experience:1'))
    await dispatch(callback_update(20, 11, f'submit:{aid}'))
    await dispatch(callback_update(20, 11, f'submit:{aid}'))  # same Telegram update
    await dispatch(callback_update(20, 12, f'submit:{aid}'))  # second click, different update
    await dispatch(callback_update(20, 13, f'adm:status:{aid}:CONTRACT'))
    async with sessions() as s:
        assert (await s.get(Application, aid)).status == 'APPLICATION'
        assert await s.scalar(select(func.count()).select_from(Event).where(Event.name == 'APPLICATION_COMPLETED', Event.user_id == 20)) == 1
        assert await s.scalar(select(func.count()).select_from(Outbox).where(Outbox.chat_id == settings().admin_id)) == 1
        for message in (await s.scalars(select(Outbox))).all():
            for row in (message.markup or {}).get('inline_keyboard', []):
                for button in row:
                    if 'callback_data' in button:
                        assert len(button['callback_data'].encode()) <= 64
    await dispatch(callback_update(settings().admin_id, 14, f'adm:status:{aid}:CONTRACT'))
    await dispatch(callback_update(settings().admin_id, 15, f'adm:status:{aid}:CONTRACT'))
    async with sessions() as s:
        assert (await s.get(Application, aid)).status == 'CONTRACT'
        assert await s.scalar(select(func.count()).select_from(Event).where(Event.name == 'CONTRACT_CONFIRMED')) == 1


async def test_deeplink_persistence_and_invalid_old_buttons(sessions, dispatch):
    await dispatch(message_update(30, 100, '/start avito_moscow_01'))
    await dispatch(message_update(30, 101, '/start vk_test'))
    await dispatch(message_update(30, 102, '/start'))
    await dispatch(callback_update(30, 103, 'answer:999:phone:0'))
    async with sessions() as s:
        user = await s.get(User, 30)
        assert (user.first_source, user.last_source, user.campaign) == ('avito', 'vk', 'vk_test')
        assert await s.scalar(select(func.count()).select_from(User).where(User.id == 30)) == 1


async def test_sheets_sync_retry_and_snapshot(sessions, monkeypatch):
    monkeypatch.setattr('app.google_sheets.Session', sessions)
    monkeypatch.setattr('app.google_sheets.settings', lambda: type('Cfg', (), {'google_sheet_id': 'test', 'google_credentials': type('Secret', (), {'get_secret_value': lambda self: 'test'})()})())
    monkeypatch.setattr('app.google_sheets.ensure_and_read', lambda: [[], []])
    captured = []
    monkeypatch.setattr('app.google_sheets.write_sheets', lambda data: captured.append(data))
    assert (await sync()).startswith('✅')
    assert captured[0]['Лиды'][1][1] == 10
    assert captured[0]['Офферы'][0] == OFFER_FIELDS
    assert captured[0]['Города'][0] == CITY_FIELDS
    def failing(data):
        raise ConnectionError('API unavailable')
    monkeypatch.setattr('app.google_sheets.write_sheets', failing)
    assert (await sync()).startswith('❌')
    async with sessions() as s:
        assert await s.get(User, 10)
    monkeypatch.setattr('app.google_sheets.write_sheets', lambda data: captured.append(data))
    assert (await sync()).startswith('✅')
    assert captured[0]['Лиды'] == captured[1]['Лиды']


def test_sheets_raw_values(monkeypatch):
    from unittest.mock import MagicMock
    api = MagicMock()
    api.get().execute.return_value = {'sheets': [{'properties': {'title': 'Лиды', 'sheetId': 1, 'gridProperties': {'rowCount': 1000, 'columnCount': 26}}}]}
    monkeypatch.setattr('app.google_sheets.client', lambda: api)
    write_sheets({'Лиды': [['Имя'], ['=IMPORTXML("bad")']]})
    kwargs = api.values().batchUpdate.call_args.kwargs
    assert kwargs['body']['valueInputOption'] == 'RAW'


async def test_blocked_recipient_stops_reminders(sessions, monkeypatch):
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage
    from app.jobs import dispatch_reminders
    from app.database import now
    monkeypatch.setattr('app.jobs.Session', sessions)
    monkeypatch.setattr('app.jobs.in_daytime', lambda user: True)
    async with sessions.begin() as s:
        user = await s.get(User, 10)
        await plan(s, user)
        await s.flush()
        (await s.scalar(select(Reminder))).scheduled_at = now()
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramForbiddenError(method=SendMessage(chat_id=10, text='test'), message='blocked')
    await dispatch_reminders(bot)
    async with sessions() as s:
        assert (await s.get(User, 10)).blocked
        assert (await s.scalar(select(Reminder))).status == 'cancelled'


async def test_transaction_failure_can_retry_same_update(sessions, monkeypatch):
    from app.database import UpdateReceipt
    monkeypatch.setattr('app.middleware.Session', sessions)
    middleware = TransactionMiddleware()
    update = message_update(80, 5000, '/start avito')
    async def fail(update, data):
        handlers.send(data['s'], 80, 'Must not be delivered')
        raise ConnectionError('temporary database failure')
    with pytest.raises(ConnectionError):
        await middleware(fail, update, {})
    async with sessions() as s:
        assert await s.get(UpdateReceipt, 5000) is None
        assert await s.get(User, 80) is None
        assert not (await s.scalars(select(Outbox).where(Outbox.chat_id == 80))).all()
    await middleware(AsyncMock(), update, {})
    async with sessions() as s:
        assert await s.get(UpdateReceipt, 5000)
        assert await s.get(User, 80)


async def test_concurrent_reminder_claim_postgres(sessions, monkeypatch):
    import asyncio
    from app.jobs import dispatch_reminders
    from app.database import now
    if sessions.kw['bind'].dialect.name != 'postgresql':
        pytest.skip('Row locking is verified on PostgreSQL')
    monkeypatch.setattr('app.jobs.Session', sessions)
    monkeypatch.setattr('app.jobs.in_daytime', lambda user: True)
    async with sessions.begin() as s:
        await plan(s, await s.get(User, 10))
        await s.flush()
        (await s.scalar(select(Reminder))).scheduled_at = now()
    bot = AsyncMock()
    await asyncio.gather(dispatch_reminders(bot), dispatch_reminders(bot))
    assert bot.send_message.await_count == 1

import asyncio
import json
import os
import subprocess
import sys
import pytest
from sqlalchemy import select
from app.config import Settings
from app.database import Application, Offer, Outbox, User
from app.services import start_application
from tests.test_integration import callback_update, message_update

pytest_plugins = []


@pytest.fixture
async def dispatch(sessions, monkeypatch):
    from aiogram import Bot
    from unittest.mock import AsyncMock
    from app.main import create_dispatcher
    monkeypatch.setattr('app.middleware.Session', sessions)
    bot = Bot('123456:TEST_TOKEN_NOT_REAL')
    bot.session = AsyncMock(return_value=True)
    dp = create_dispatcher()
    yield lambda update: dp.feed_update(bot, update)
    await bot.session.close()


@pytest.mark.parametrize('action', ['admin', 'adm:new:0', 'adm:leads:0', 'adm:stats:0', 'adm:sources', 'adm:contact:{id}', 'adm:comment:{id}', 'adm:status:{id}:CONTRACT', 'adm:status:{id}:WAITING', 'adm:status:{id}:REJECTED', 'adm:toggle:offers:first', 'adm:offerurl:first', 'adm:sync', 'edit:{id}', 'submit:{id}', 'partner:{id}'])
async def test_outsider_cannot_access_candidates(sessions, dispatch, action):
    async with sessions.begin() as s:
        owner = await s.get(User, 10)
        app = await start_application(s, owner, await s.get(Offer, 'first'))
        app.answers = {'name': 'PRIVATE_CANDIDATE', 'phone': '+79998887766'}
        aid = app.id
    await dispatch(callback_update(777, 8800, action.format(id=aid)))
    async with sessions() as s:
        app = await s.get(Application, aid)
        assert app.status == 'APPLICATION_STARTED'
        assert app.comment == ''
        assert (await s.get(Offer, 'first')).active
        for msg in (await s.scalars(select(Outbox).where(Outbox.chat_id == 777))).all():
            assert 'PRIVATE_CANDIDATE' not in msg.text
            assert '+79998887766' not in msg.text
            assert 'Новые заявки' not in json.dumps(msg.markup, ensure_ascii=False)


async def test_admin_functions_without_google(sessions, dispatch):
    async with sessions.begin() as s:
        app = await start_application(s, await s.get(User, 10), await s.get(Offer, 'first'))
        app.answers = {'name': 'Иван Иванов', 'phone': '+79998887766'}
        app.status = 'APPLICATION'
        aid = app.id
    actions = ['admin', 'adm:new:0', 'adm:leads:0', 'adm:stats:0', 'adm:sources', f'adm:contact:{aid}', f'adm:status:{aid}:WAITING', f'adm:status:{aid}:REJECTED', f'adm:status:{aid}:CONTRACT', 'adm:offerurl:first']
    for number, action in enumerate(actions, 9000):
        await dispatch(callback_update(1132113524, number, action))
    await dispatch(message_update(1132113524, 9100, 'https://example.com/partner'))
    async with sessions() as s:
        assert (await s.get(Application, aid)).status == 'CONTRACT'
        assert (await s.get(Offer, 'first')).partner_url == 'https://example.com/partner'
        messages = (await s.scalars(select(Outbox).where(Outbox.chat_id == 1132113524))).all()
        assert any('Иван Иванов' in m.text and '+79998887766' in m.text for m in messages)
        assert any('Зашли:' in m.text for m in messages)
        assert all('Синхронизировать CRM' not in json.dumps(m.markup, ensure_ascii=False) for m in messages)


def test_admin_id_is_restricted():
    with pytest.raises(ValueError):
        Settings(_env_file=None, admin_id=777)


async def test_minimal_env_actual_startup_without_google(sessions, tmp_path):
    """Fresh interpreter, only 3 env settings, actual seed/DB/HTTP/polling; Telegram mocked."""
    url = sessions.kw['bind'].url.render_as_string(hide_password=False)
    (tmp_path / '.env').write_text(f'BOT_TOKEN=123456:TEST_TOKEN_NOT_REAL\nADMIN_ID=1132113524\nDATABASE_URL={url}\n', encoding='utf-8')
    code = r'''
import asyncio, importlib.abc, sys
class NoGoogle(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'google', 'googleapiclient', 'google_auth_httplib2', 'httplib2'}:
            raise ModuleNotFoundError('Google is intentionally unavailable')
sys.meta_path.insert(0, NoGoogle())
from unittest.mock import AsyncMock
from contextlib import suppress
from aiogram import Bot
from app import main
from app.config import settings
from app.database import Session, User
import aiohttp
async def test():
    cfg = settings()
    assert cfg.google_sheet_id == '' and cfg.google_credentials.get_secret_value() == ''
    assert cfg.mode == 'polling' and cfg.scheduler_mode == 'internal'
    bot = Bot(cfg.bot_token.get_secret_value())
    bot.session = AsyncMock(return_value=True)
    called = asyncio.Event()
    async def get_updates(**kwargs):
        if not called.is_set():
            called.set()
            from aiogram.types import Update
            return [Update.model_validate({'update_id': 778899, 'message': {'message_id': 1, 'date': 1700000000, 'chat': {'id': 88888, 'type': 'private'}, 'from': {'id': 88888, 'is_bot': False, 'first_name': 'Test'}, 'text': '/start avito'}})]
        await asyncio.Event().wait()
    bot.get_updates = get_updates
    main.Bot = lambda token: bot
    task = asyncio.create_task(main.run())
    try:
        await asyncio.wait_for(called.wait(), 10)
        for _ in range(100):
            async with Session() as s:
                user = await s.get(User, 88888)
                if user:
                    assert user.first_source == 'avito'
                    break
            await asyncio.sleep(.03)
        else:
            raise AssertionError('Polling update not persisted')
        async with aiohttp.ClientSession() as client:
            async with client.get('http://127.0.0.1:8080/health') as r:
                assert r.status == 200
            async with client.post('http://127.0.0.1:8080/telegram', json={}) as r:
                assert r.status == 404
        assert 'app.google_sheets' not in sys.modules
        print('NO_GOOGLE_STARTUP_OK')
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
asyncio.run(test())
'''
    env = dict(os.environ)
    for key in list(env):
        if key.lower() in Settings.model_fields:
            env.pop(key)
    env['PYTHONPATH'] = os.pathsep.join(str(p) for p in sys.path if p)
    result = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'NO_GOOGLE_STARTUP_OK' in result.stdout

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import func, select
from app.database import Application, Event, Offer, Reminder, User, now
from app.services import clicked, in_daytime, phone, plan, set_status, source, start_application, submit
from app.google_sheets import CITY_FIELDS, OFFER_FIELDS, parse_catalog
from app.handlers import save_answer
from app.jobs import dispatch_reminders


def test_phone_and_source():
    assert phone('8 (999) 123-45-67') == '+79991234567'
    assert phone('+7 999 123 45 67') == '+79991234567'
    for bad in ('hello', '123', '+000123456789', '+7<script>'):
        with pytest.raises(ValueError):
            phone(bad)
    assert source('avito_moscow_01') == ('avito', 'avito_moscow_01')
    assert source('unknown_campaign') == ('unknown', 'unknown_campaign')
    assert source('!!!') == ('direct', 'direct')


async def test_fsm_restart_edit_submit_and_duplicates(sessions):
    async with sessions.begin() as s:
        user, offer = await s.get(User, 10), await s.get(Offer, 'first')
        app = await start_application(s, user, offer)
        app_id = app.id
        assert (await start_application(s, user, offer)).id == app.id
        await save_answer(s, user, app, 'Иван')
        with pytest.raises(ValueError):
            await save_answer(s, user, app, 'двадцать')
        assert app.step == 'age'
    # A new DB session represents process restart; state is entirely in DB.
    async with sessions.begin() as s:
        user, app = await s.get(User, 10), await s.get(Application, app_id)
        assert app.step == 'age' and app.answers['name'] == 'Иван'
        for value in ['20', 'Москва', 'Центр', '+79991234567', '🟢 Могу начать скоро', '❌ Без опыта']:
            await save_answer(s, user, app, value)
        assert app.step == 'review'
        app.step, app.editing = 'district', True
        await save_answer(s, user, app, 'Арбат')
        assert app.step == 'review'
        assert await submit(s, user, app)
        assert not await submit(s, user, app)
        assert app.consent_at and app.status == 'APPLICATION'
        app2 = await start_application(s, user, await s.get(Offer, 'second'))
        assert app2.id != app.id
        assert await s.scalar(select(func.count()).select_from(User)) == 1


async def test_plan_idempotency_and_closed_status(sessions):
    async with sessions.begin() as s:
        user = await s.get(User, 10)
        await plan(s, user)
        await s.flush()
        await plan(s, user)
        assert await s.scalar(select(func.count()).select_from(Reminder)) == 1
        app = await start_application(s, user, await s.get(Offer, 'first'))
        await s.flush()
        assert len((await s.scalars(select(Reminder).where(Reminder.status == 'pending'))).all()) == 2
        await set_status(s, user, app, 'REJECTED', 999)
        assert not (await s.scalars(select(Reminder).where(Reminder.status == 'pending'))).all()


async def test_click_cancels_application_reminder(sessions):
    async with sessions.begin() as s:
        user = await s.get(User, 10)
        app = await start_application(s, user, await s.get(Offer, 'first'))
        app.submitted_at, app.status, user.status = now(), 'APPLICATION', 'APPLICATION'
        await plan(s, user, app)
        await s.flush()
        await clicked(s, user, app)
        await s.flush()
        pending = (await s.scalars(select(Reminder).where(Reminder.status == 'pending'))).all()
        assert [r.reminder_type for r in pending] == ['clicked']
        await clicked(s, user, app)
        assert await s.scalar(select(func.count()).select_from(Event).where(Event.name == 'PARTNER_LINK_CLICK')) == 1


async def test_scheduler_no_double_delivery_and_optout(sessions, monkeypatch):
    monkeypatch.setattr('app.jobs.Session', sessions)
    monkeypatch.setattr('app.jobs.in_daytime', lambda user: True)
    async with sessions.begin() as s:
        user = await s.get(User, 10)
        await plan(s, user)
        await s.flush()
        r = await s.scalar(select(Reminder))
        r.scheduled_at = now() - timedelta(hours=1)
    bot = SimpleNamespace(send_message=AsyncMock())
    await dispatch_reminders(bot)
    await dispatch_reminders(bot)
    assert bot.send_message.await_count == 1
    async with sessions.begin() as s:
        user = await s.get(User, 10)
        user.reminders_enabled = False
        app = await start_application(s, user, await s.get(Offer, 'first'))
        await plan(s, user, app)
        assert not (await s.scalars(select(Reminder).where(Reminder.status == 'pending'))).all()


async def test_ambiguous_send_is_not_retried(sessions, monkeypatch):
    monkeypatch.setattr('app.jobs.Session', sessions)
    monkeypatch.setattr('app.jobs.in_daytime', lambda user: True)
    async with sessions.begin() as s:
        user = await s.get(User, 10)
        await plan(s, user)
        await s.flush()
        (await s.scalar(select(Reminder))).scheduled_at = now() - timedelta(hours=1)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError))
    await dispatch_reminders(bot)
    await dispatch_reminders(bot)
    assert bot.send_message.await_count == 1
    async with sessions() as s:
        assert (await s.scalar(select(Reminder))).status == 'unknown'


def test_daytime_and_unknown_timezone():
    at = now().replace(hour=8, minute=0)
    assert in_daytime(SimpleNamespace(timezone=None), at)
    assert not in_daytime(SimpleNamespace(timezone=None), at.replace(hour=18))
    assert not in_daytime(SimpleNamespace(timezone='Europe/Moscow'), at.replace(hour=23))


def test_catalog_validation():
    good = [CITY_FIELDS, ['moscow', 'Москва', 'TRUE', '1', 'Europe/Moscow']]
    assert parse_catalog(good, CITY_FIELDS)[0]['active']
    with pytest.raises(ValueError):
        parse_catalog([CITY_FIELDS, ['bad:id', 'Город', 'TRUE', 0, 'UTC']], CITY_FIELDS)
    with pytest.raises(ValueError):
        parse_catalog([['wrong'], ['x']], OFFER_FIELDS)


async def test_attribution_requires_open(sessions):
    async with sessions.begin() as s:
        user = await s.get(User, 10)
        app = await start_application(s, user, await s.get(Offer, 'first'))
        r = Reminder(dedup_key='attribution', user_id=10, application_id=app.id, reminder_type='draft', stage='APPLICATION_STARTED', scheduled_at=now(), status='sent', sent_at=now(), opened_at=now())
        s.add(r)
        app.answers = dict(name='Иван', age=20, city='Москва', district='Центр', phone='+79991234567', readiness='Скоро', experience='Нет')
        app.step = 'review'
        await submit(s, user, app)
        assert r.converted_at

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock
import aiohttp
from sqlalchemy import select
from app.config import Settings
from app.database import Application, Event, now


async def test_webhook_health_redirect_and_cron(sessions, monkeypatch):
    from app import main
    monkeypatch.setattr(main, 'Session', sessions)
    monkeypatch.setattr('app.middleware.Session', sessions)
    monkeypatch.setattr('app.jobs.Session', sessions)
    cfg = Settings(_env_file=None, bot_token='123456:TEST_TOKEN_NOT_REAL', mode='webhook', public_url='https://example.test', port=18083, scheduler_mode='external', webhook_secret='w' * 32, cron_secret='c' * 32, link_secret='l' * 32)
    monkeypatch.setattr(main, 'settings', lambda: cfg)
    monkeypatch.setattr('app.services.settings', lambda: cfg)
    monkeypatch.setattr(main, 'seed', AsyncMock())
    from aiogram import Bot
    bot = Bot(cfg.bot_token.get_secret_value())
    bot.session = AsyncMock(return_value=True)
    monkeypatch.setattr(main, 'Bot', lambda token: bot)
    task = asyncio.create_task(main.run())
    try:
        async with aiohttp.ClientSession() as client:
            for _ in range(100):
                if task.done():
                    await task
                try:
                    async with client.get('http://127.0.0.1:18083/health') as r:
                        assert r.status == 200
                    break
                except aiohttp.ClientConnectionError:
                    await asyncio.sleep(0.02)
            else:
                raise AssertionError('Server did not start')
            async with client.post('http://127.0.0.1:18083/telegram', json={}) as r:
                assert r.status == 401
            async with client.post('http://127.0.0.1:18083/jobs/tick') as r:
                assert r.status == 401
            async with client.post('http://127.0.0.1:18083/jobs/tick', headers={'Authorization': 'Bearer ' + 'c' * 32}) as r:
                assert r.status == 200
            from tests.test_integration import message_update
            payload = message_update(60, 1000, '/start telegram').model_dump(mode='json', exclude_none=True)
            async with client.post('http://127.0.0.1:18083/telegram', json=payload, headers={'X-Telegram-Bot-Api-Secret-Token': 'w' * 32}) as r:
                assert r.status == 200
            async with sessions.begin() as s:
                from app.database import Offer
                offer = await s.get(Offer, 'first')
                offer.partner_url = 'https://example.com/apply'
                app = Application(user_id=60, offer_id='first', partner='Partner', vacancy='Courier', source='telegram', campaign='telegram', status='APPLICATION', step='review', answers={}, submitted_at=now())
                s.add(app)
                await s.flush()
                aid = app.id
            from app.services import signature
            url = f'http://127.0.0.1:18083/go/{aid}/{signature(aid)}'
            async with client.get(url) as r:
                assert r.status == 200
                assert 'Смена рядом' in await r.text()
            async with sessions() as s:
                assert (await s.get(Application, aid)).clicked_at is None
            async with client.post(url, allow_redirects=False) as r:
                assert r.status == 303
                assert r.headers['Location'] == 'https://example.com/apply'
            async with sessions() as s:
                assert (await s.get(Application, aid)).clicked_at
                assert await s.scalar(select(Event).where(Event.name == 'PARTNER_LINK_CLICK'))
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

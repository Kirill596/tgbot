import asyncio
import hmac
import logging
import signal
import structlog
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.types import Update
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, text
from app import admin, handlers
from app.config import settings
from app.database import Application, Offer, Session, User, engine
from app.jobs import drain_outbox, recover_claims, tick
from app.middleware import TransactionMiddleware
from app.seed import seed
from app.services import CLOSED, clicked, signature, valid_url

log = structlog.get_logger()


def background(coro, label):
    task = asyncio.create_task(coro)

    def done(completed):
        try:
            completed.result()
        except Exception as exc:
            log.error('background_failed', task=label, error_type=type(exc).__name__)

    task.add_done_callback(done)


def create_dispatcher():
    dp = Dispatcher()
    dp.update.outer_middleware(TransactionMiddleware())
    router = Router()
    router.message.register(handlers.start, CommandStart())
    router.callback_query.register(admin.admin_callback, F.data.startswith('adm:'))
    router.callback_query.register(handlers.callback)
    router.message.register(admin.admin_text, F.text)
    router.message.register(handlers.message_handler)
    dp.include_router(router)
    return dp


async def run():
    cfg = settings()
    if not cfg.bot_token.get_secret_value():
        raise RuntimeError('Заполните BOT_TOKEN в .env или переменных хостинга.')
    logging.basicConfig(level=logging.WARNING, format='%(message)s')
    structlog.configure(processors=[structlog.processors.TimeStamper(fmt='iso'), structlog.processors.JSONRenderer()], logger_factory=structlog.PrintLoggerFactory())
    # A short API timeout prevents a temporary Telegram network failure from freezing polling.
    bot = Bot(cfg.bot_token.get_secret_value(), session=AiohttpSession(timeout=7))
    dp = create_dispatcher()
    await seed()
    await recover_claims()
    stop = asyncio.Event()

    async def after_update():
        # One committed update produces one coalesced UI screen. Process it
        # immediately and independently from other users.
        background(drain_outbox(bot, limit=1), 'outbox')

    async def health(request):
        # Liveness must not keep a serverless database awake and consume its free quota.
        return web.json_response({'status': 'ok'})

    async def webhook(request):
        if not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token', ''), cfg.webhook_secret.get_secret_value()):
            raise web.HTTPUnauthorized()
        try:
            update = Update.model_validate(await request.json(), context={'bot': bot})
            await dp.feed_update(bot, update)
        except Exception as exc:
            log.error('webhook_failed', error_type=type(exc).__name__)
            return web.Response(status=503)
        try:
            await after_update()
        except Exception as exc:
            log.error('outbox_deferred', error_type=type(exc).__name__)
        return web.Response(text='ok')

    async def cron(request):
        expected = 'Bearer ' + cfg.cron_secret.get_secret_value()
        if not cfg.cron_secret.get_secret_value() or not hmac.compare_digest(request.headers.get('Authorization', ''), expected):
            raise web.HTTPUnauthorized()
        healthy = await tick(bot)
        return web.Response(text='ok' if healthy else 'retry later', status=200 if healthy else 503)

    async def redirect(request):
        try:
            app_id = int(request.match_info['app_id'])
        except ValueError:
            raise web.HTTPNotFound()
        if not hmac.compare_digest(request.match_info['token'], signature(app_id)):
            raise web.HTTPNotFound()
        async with Session.begin() as s:
            app = await s.get(Application, app_id)
            if not app or not app.submitted_at or app.status in CLOSED:
                raise web.HTTPNotFound()
            offer = await s.get(Offer, app.offer_id)
            if not offer or not offer.active or not valid_url(offer.partner_url):
                raise web.HTTPGone(text='Оформление недоступно. Вернитесь в бот.')
            if request.method == 'GET':
                # Link-preview GET requests do not count as candidate clicks.
                return web.Response(text='<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Смена рядом</title><body style="font:20px system-ui;max-width:480px;margin:15vh auto;padding:24px"><h1>Смена рядом</h1><p>Следующий шаг — оформление на сайте партнёра.</p><form method="post"><button style="padding:16px;border:0;border-radius:12px;background:#16865d;color:white;font:inherit">Перейти к оформлению</button></form></body></html>', content_type='text/html', headers={'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"})
            if s.bind.dialect.name == 'postgresql':
                await s.execute(text('SELECT pg_advisory_xact_lock(:uid)'), {'uid': app.user_id})
            user = await s.scalar(select(User).where(User.id == app.user_id).with_for_update())
            await s.refresh(app)
            if app.status in CLOSED:
                raise web.HTTPGone()
            await clicked(s, user, app)
            url = offer.partner_url
        raise web.HTTPSeeOther(url, headers={'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store'})

    webapp = web.Application(client_max_size=1024 * 1024)
    webapp.router.add_get('/health', health)
    if cfg.mode == 'webhook':
        webapp.router.add_post('/telegram', webhook)
    webapp.router.add_post('/jobs/tick', cron)
    webapp.router.add_get('/go/{app_id}/{token}', redirect, allow_head=False)
    webapp.router.add_post('/go/{app_id}/{token}', redirect)
    runner = web.AppRunner(webapp, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', cfg.port).start()
    scheduler = AsyncIOScheduler(timezone='UTC')
    if cfg.scheduler_mode == 'internal':
        scheduler.add_job(tick, 'interval', seconds=30, args=[bot], max_instances=1, coalesce=True)
        scheduler.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    log.info('started', mode=cfg.mode)
    try:
        if cfg.mode == 'webhook':
            await bot.set_webhook(cfg.public_url.rstrip('/') + '/telegram', secret_token=cfg.webhook_secret.get_secret_value(), allowed_updates=['message', 'callback_query'], drop_pending_updates=False)
            await stop.wait()
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            offset = None
            # Sequential polling acknowledges each update only after DB commit.
            while not stop.is_set():
                try:
                    # Long polling intentionally waits longer than the short send-message timeout.
                    updates = await bot.get_updates(offset=offset, timeout=25, request_timeout=35, allowed_updates=['message', 'callback_query'])
                    for update in updates:
                        await dp.feed_update(bot, update)
                        offset = update.update_id + 1
                        await after_update()
                except Exception as exc:
                    log.error('polling_retry', error_type=type(exc).__name__)
                    await asyncio.sleep(3)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()
        await engine.dispose()
        log.info('stopped')


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass

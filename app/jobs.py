import asyncio
from datetime import timedelta
import structlog
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from sqlalchemy import select, text
from app.config import settings
from app.database import Application, Offer, Outbox, Reminder, Session, User, now
from app.keyboards import inline
from app.services import CLOSED, event, in_daytime, utc
from app.texts import REMINDERS

log = structlog.get_logger()
worker_lock = asyncio.Lock()


def markup(raw):
    if not raw:
        return None
    if 'inline_keyboard' in raw:
        return InlineKeyboardMarkup.model_validate(raw)
    if 'keyboard' in raw:
        return ReplyKeyboardMarkup.model_validate(raw)
    return ReplyKeyboardRemove.model_validate(raw)


async def drain_outbox(bot, limit=30):
    # PostgreSQL SKIP LOCKED claims a single row. Do not serialize every user
    # behind one slow Telegram request: each incoming update can drain its own reply.
    for _ in range(limit):
        async with Session.begin() as s:
            item = await s.scalar(select(Outbox).where(Outbox.status == 'pending', Outbox.due_at <= now()).order_by(Outbox.id).with_for_update(skip_locked=True).limit(1))
            if not item:
                break
            item.status = 'sending'
            item.attempts += 1
            iid, chat_id, body, keys, kind = item.id, item.chat_id, item.text, item.markup, item.kind
        try:
            sent_message_id = None
            used_edit = False
            if kind == 'ui':
                async with Session() as lookup:
                    user = await lookup.get(User, chat_id)
                    previous_id = user.ui_message_id if user else None
                # Telegram only supports editing inline keyboards; reply keyboards require a fresh message.
                if previous_id and not (keys and 'keyboard' in keys):
                    try:
                        await bot.edit_message_text(chat_id=chat_id, message_id=previous_id, text=body, reply_markup=markup(keys))
                        used_edit = True
                    except TelegramBadRequest as exc:
                        if 'message is not modified' in str(exc).lower():
                            used_edit = True
                        else:
                            log.info('ui_edit_fallback', error_type=type(exc).__name__)
                if not used_edit:
                    message = await bot.send_message(chat_id, body, reply_markup=markup(keys))
                    sent_message_id = message.message_id
                    if previous_id:
                        try:
                            await bot.delete_message(chat_id, previous_id)
                        except TelegramBadRequest:
                            pass
            else:
                await bot.send_message(chat_id, body, reply_markup=markup(keys))
            status, delay = 'sent', 0
        except TelegramRetryAfter as exc:
            status, delay = 'pending', exc.retry_after + 1
        except TelegramForbiddenError:
            status, delay = 'blocked', 0
            async with Session.begin() as s:
                user = await s.get(User, chat_id)
                if user:
                    user.blocked = True
        except TelegramBadRequest:
            status, delay = 'failed', 0
            log.warning('telegram_bad_request')
        except Exception as exc:
            status, delay = 'unknown', 0
            log.warning('telegram_ambiguous_delivery', error_type=type(exc).__name__)
        async with Session.begin() as s:
            item = await s.get(Outbox, iid)
            item.status, item.due_at = status, now() + timedelta(seconds=delay)
            if status == 'sent' and kind == 'ui' and sent_message_id:
                user = await s.get(User, chat_id)
                if user:
                    user.ui_message_id = sent_message_id


def reminder_valid(user, app, r):
    if not user.reminders_enabled or user.blocked or user.unanswered >= settings().reminder_max_unanswered:
        return False
    if user.status in CLOSED:
        return False
    if app:
        if user.current_application != app.id or app.status in CLOSED:
            return False
        if r.reminder_type.startswith('draft'):
            return not app.submitted_at and not app.clicked_at
        if r.reminder_type == 'application':
            return bool(app.submitted_at and not app.clicked_at)
        return r.reminder_type == 'clicked' and bool(app.clicked_at)
    return user.current_application is None and user.status == r.stage


async def dispatch_reminders(bot, limit=20):
    # Claim + durable sending marker before Telegram. Ambiguous sends are never automatically retried.
    for _ in range(limit):
        async with Session.begin() as s:
            r = await s.scalar(select(Reminder).where(Reminder.status == 'pending', Reminder.scheduled_at <= now()).order_by(Reminder.scheduled_at).with_for_update(skip_locked=True).limit(1))
            if not r:
                break
            user = await s.get(User, r.user_id)
            app = await s.get(Application, r.application_id) if r.application_id else None
            if not reminder_valid(user, app, r):
                r.status, r.cancelled_at = 'cancelled', now()
                continue
            if not in_daytime(user):
                r.scheduled_at = now() + timedelta(minutes=30)
                continue
            gap = timedelta(hours=settings().reminder_min_gap_hours)
            if user.last_reminder and now() - utc(user.last_reminder) < gap:
                r.scheduled_at = utc(user.last_reminder) + gap
                continue
            if r.reminder_type == 'draft_second':
                first = await s.scalar(select(Reminder).where(Reminder.user_id == user.id, Reminder.application_id == r.application_id, Reminder.reminder_type == 'draft'))
                if not first or first.status not in {'sent', 'unknown'}:
                    r.scheduled_at = now() + timedelta(hours=1)
                    continue
                if first.sent_at and now() - utc(first.sent_at) < timedelta(hours=settings().reminder_hours_second):
                    r.scheduled_at = utc(first.sent_at) + timedelta(hours=settings().reminder_hours_second)
                    continue
            r.status, r.result = 'sending', 'claimed'
            rid, uid = r.id, user.id
        # Same advisory lock as user updates: revalidate after claim, serialize against submit/opt-out.
        async with Session.begin() as s:
            if s.bind.dialect.name == 'postgresql':
                await s.execute(text('SELECT pg_advisory_xact_lock(:uid)'), {'uid': uid})
            user = await s.scalar(select(User).where(User.id == uid).with_for_update())
            r = await s.get(Reminder, rid)
            app = await s.get(Application, r.application_id) if r.application_id else None
            if not reminder_valid(user, app, r):
                r.status, r.cancelled_at = 'cancelled', now()
                continue
            offer = await s.get(Offer, app.offer_id if app else user.viewed_offer) if app or user.viewed_offer else None
            if offer and not offer.active:
                r.status, r.cancelled_at = 'cancelled', now()
                continue
            body = REMINDERS[r.reminder_type].format(vacancy=app.vacancy if app else offer.vacancy_name if offer else 'подработка')
            label = {'new': '🔎 Посмотреть вакансии', 'interested': '🚀 Продолжить', 'draft': '➡️ Продолжить анкету', 'draft_second': 'Продолжить', 'application': '🚀 Продолжить оформление', 'clicked': '❓ Есть вопрос'}[r.reminder_type]
            rows = [[(label, f'rem:{r.id}')]]
            if r.reminder_type == 'interested':
                rows.append([('💰 Ещё раз посмотреть условия', f'rem:{r.id}:conditions')])
            rows.append([('🔕 Не напоминать', 'disable')])
            try:
                await bot.send_message(uid, body, reply_markup=inline(*rows))
                r.status, r.sent_at, r.result = 'sent', now(), 'delivered'
                user.last_reminder, user.unanswered = now(), user.unanswered + 1
                event(s, user, 'REMINDER_SENT', app, reminder_id=r.id, type=r.reminder_type, stage=r.stage)
                log.info('reminder_sent', reminder_type=r.reminder_type)
            except TelegramForbiddenError:
                user.blocked = True
                r.status, r.result = 'cancelled', 'blocked'
                r.cancelled_at = now()
            except TelegramRetryAfter as exc:
                r.status, r.scheduled_at = 'pending', now() + timedelta(seconds=exc.retry_after + 1)
            except Exception as exc:
                r.status, r.result = 'unknown', type(exc).__name__
                user.last_reminder, user.unanswered = now(), user.unanswered + 1
                log.warning('reminder_delivery_unknown', error_type=type(exc).__name__)


async def recover_claims():
    # Run once at startup; single web replica required. A crash may lose one message, never resend it blindly.
    async with Session.begin() as s:
        for model in (Outbox, Reminder):
            for row in (await s.scalars(select(model).where(model.status == 'sending'))).all():
                row.status = 'unknown'


async def tick(bot):
    if worker_lock.locked():
        return True
    async with worker_lock:
        try:
            await drain_outbox(bot)
            await dispatch_reminders(bot)
            return True
        except Exception as exc:
            log.error('scheduler_failed', error_type=type(exc).__name__)
            return False

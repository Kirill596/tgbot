import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, text
from app.database import Session, UpdateReceipt, User, now
from app.handlers import send
from app.keyboards import home
from app.services import source

log = structlog.get_logger()


class TransactionMiddleware(BaseMiddleware):
    async def __call__(self, handler, update, data):
        obj = update.callback_query or update.message
        if not isinstance(obj, (Message, CallbackQuery)) or not obj.from_user:
            return
        if isinstance(obj, Message) and obj.chat.type != 'private':
            return
        if isinstance(obj, CallbackQuery) and obj.message and obj.message.chat.type != 'private':
            return
        uid = obj.from_user.id
        if isinstance(obj, CallbackQuery):
            try:
                await obj.answer()
            except Exception:
                pass  # Expired callback/removed message does not invalidate persisted state.
        try:
            async with Session.begin() as s:
                if s.bind.dialect.name == 'postgresql':
                    await s.execute(text('SELECT pg_advisory_xact_lock(:uid)'), {'uid': uid})
                if await s.get(UpdateReceipt, update.update_id):
                    return
                s.add(UpdateReceipt(id=update.update_id))
                user = await s.scalar(select(User).where(User.id == uid).with_for_update())
                if user is None:
                    payload = obj.text.split(maxsplit=1) if isinstance(obj, Message) and obj.text and obj.text.startswith('/start') else []
                    src, campaign = source(payload[1] if len(payload) == 2 else '')
                    user = User(id=uid, first_source=src, last_source=src, campaign=campaign,
                                status='NEW', reminders_enabled=True, unanswered=0)
                    s.add(user)
                    await s.flush()
                    log.info('lead_created')
                user.username, user.last_visit, user.blocked = obj.from_user.username, now(), False
                user.unanswered = 0
                # A callback always identifies the exact bot message the user expects to change.
                # The outbox will edit that message after the transaction commits.
                if isinstance(obj, CallbackQuery) and obj.message:
                    user.ui_message_id = obj.message.message_id
                data.update(s=s, user=user)
                try:
                    result = await handler(update, data)
                except (ValueError, IndexError) as exc:
                    send(s, uid, str(exc) if isinstance(exc, ValueError) else 'Кнопка устарела. Открой меню.', home())
                    result = None
            return result
        except Exception as exc:
            # Deliberately omit exception text: DB errors may embed SQL values or secrets.
            log.error('update_failed', error_type=type(exc).__name__)
            raise  # Webhook returns 5xx so Telegram retries; transaction was rolled back.

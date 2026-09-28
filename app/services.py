import hashlib
import hmac
import re
import structlog
from datetime import timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.config import settings
from app.database import Application, Event, Offer, Reminder, StatusHistory, now

STEPS = ['name', 'age', 'city', 'district', 'phone', 'readiness', 'experience', 'review']
STATUSES = ['NEW', 'INTERESTED', 'APPLICATION_STARTED', 'APPLICATION', 'CONTRACT', 'WAITING', 'REJECTED', 'NOT_RELEVANT', 'DUPLICATE']
CLOSED = {'CONTRACT', 'REJECTED', 'NOT_RELEVANT', 'DUPLICATE'}
log = structlog.get_logger()


def utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def source(payload):
    payload = payload if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', payload or '') else 'direct'
    return payload.split('_')[0].lower(), payload


def phone(value):
    digits = re.sub(r'[\s()\-]', '', value)
    if re.fullmatch(r'8\d{10}', digits):
        digits = '+7' + digits[1:]
    elif re.fullmatch(r'7\d{10}', digits):
        digits = '+' + digits
    if not re.fullmatch(r'\+[1-9]\d{9,14}', digits):
        raise ValueError('Введи телефон с кодом страны, например +7 999 123-45-67.')
    return digits


def valid_url(value):
    parsed = urlparse(value)
    return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username


def signature(app_id):
    secret = settings().link_secret.get_secret_value() or settings().bot_token.get_secret_value()
    return hmac.new(secret.encode(), str(app_id).encode(), hashlib.sha256).hexdigest()


def event(s, user, name, app=None, **details):
    details = {'source': user.first_source, 'campaign': app.campaign if app else user.campaign,
               'city': app.answers.get('city', user.city) if app else user.city,
               'offer': app.offer_id if app else user.viewed_offer or '',
               'partner': app.partner if app else '', **details}
    s.add(Event(user_id=user.id, application_id=app.id if app else None, name=name, details=details))


async def plan(s, user, app=None):
    """Reconcile persisted jobs. Unique keys cap each stage across restarts."""
    cfg = settings()
    stage = app.status if app else user.status
    wanted = []
    if user.reminders_enabled and not user.blocked and stage not in CLOSED:
        if app and app.clicked_at:
            wanted = [('clicked', cfg.reminder_hours_clicked)]
        elif app and app.submitted_at:
            wanted = [('application', cfg.reminder_hours_application)]
        elif app:
            wanted = [('draft', cfg.reminder_hours_draft), ('draft_second', cfg.reminder_hours_draft + cfg.reminder_hours_second)]
        elif stage == 'INTERESTED':
            wanted = [('interested', cfg.reminder_hours_interested)]
        elif stage == 'NEW':
            wanted = [('new', cfg.reminder_hours_new)]
    jobs = list((await s.scalars(select(Reminder).where(Reminder.user_id == user.id))).all())
    keys = {f'{user.id}:{app.id if app else 0}:{kind}' for kind, _ in wanted}
    for job in jobs:
        if job.status == 'pending' and job.dedup_key not in keys:
            job.status, job.cancelled_at = 'cancelled', now()
    for kind, delay in wanted:
        key = f'{user.id}:{app.id if app else 0}:{kind}'
        existing = next((j for j in jobs if j.dedup_key == key), None)
        if existing is None:
            s.add(Reminder(dedup_key=key, user_id=user.id, application_id=app.id if app else None,
                           reminder_type=kind, stage=stage, scheduled_at=now() + timedelta(hours=delay)))
        elif existing.status == 'pending':
            existing.scheduled_at = now() + timedelta(hours=delay)


def in_daytime(user, at=None):
    at = at or now()
    if user.timezone:
        hour = at.astimezone(ZoneInfo(user.timezone)).hour
        return 10 <= hour < 20
    # All Russian zones UTC+2..UTC+12: 08:00–09:00 UTC is 10:00–21:00 local.
    return at.hour == 8


async def set_status(s, user, app, status, actor):
    if status not in STATUSES:
        raise ValueError('Неизвестный статус')
    if status == app.status:
        return
    old = app.status
    app.status, app.updated_at = status, now()
    if user.current_application == app.id:
        user.status = status
    s.add(StatusHistory(application_id=app.id, old_status=old, new_status=status, actor=actor))
    event(s, user, 'STATUS_CHANGED', app, old=old, new=status)
    log.info('status_changed', old=old, new=status)
    if status == 'CONTRACT':
        event(s, user, 'CONTRACT_CONFIRMED', app)
    # Closed applications always cancel their jobs, regardless of current application.
    for r in (await s.scalars(select(Reminder).where(Reminder.application_id == app.id, Reminder.status == 'pending'))).all():
        r.status, r.cancelled_at = 'cancelled', now()
    if user.current_application == app.id:
        await plan(s, user, app)


async def start_application(s, user, offer):
    app = await s.scalar(select(Application).where(Application.user_id == user.id, Application.offer_id == offer.offer_id))
    if app is None:
        app = Application(user_id=user.id, offer_id=offer.offer_id, partner=offer.partner,
                          vacancy=offer.vacancy_name, source=user.last_source, campaign=user.campaign,
                          answers={}, status='APPLICATION_STARTED', step='name')
        s.add(app)
        await s.flush()
        event(s, user, 'APPLICATION_STARTED', app)
    user.current_application, user.status = app.id, app.status
    user.ui = 'form' if not app.submitted_at and app.status not in CLOSED else 'menu'
    await plan(s, user, app)
    return app


async def submit(s, user, app):
    if app.submitted_at:
        return False
    if app.step != 'review' or any(k not in app.answers for k in STEPS[:-1]):
        raise ValueError('Сначала закончи анкету.')
    offer = await s.get(Offer, app.offer_id)
    if not offer or not offer.active:
        raise ValueError('Вакансия больше неактивна. Выбери другую в меню.')
    app.submitted_at = app.consent_at = now()
    app.consent_text = settings().consent_text
    await set_status(s, user, app, 'APPLICATION', user.id)
    user.ui = 'menu'
    event(s, user, 'APPLICATION_COMPLETED', app)
    log.info('application_completed')
    # Attribution requires a reminder button click, same application (or pre-application), <= 7 days.
    rows = (await s.scalars(select(Reminder).where(Reminder.user_id == user.id, Reminder.opened_at.is_not(None), Reminder.converted_at.is_(None)).order_by(Reminder.opened_at.desc()))).all()
    for r in rows:
        if r.application_id in (None, app.id) and now() - utc(r.opened_at) <= timedelta(days=7):
            r.converted_at, r.result = now(), 'application'
            event(s, user, 'REMINDER_CONVERTED', app, reminder_id=r.id)
            break
    return True


async def clicked(s, user, app):
    if not app.clicked_at:
        app.clicked_at = now()
        event(s, user, 'PARTNER_LINK_CLICK', app)
        await plan(s, user, app)

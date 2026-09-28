from collections import defaultdict
from datetime import timedelta
from sqlalchemy import select
from app.database import Application, Event, now

FUNNEL = [('BOT_OPEN', 'Зашли'), ('OFFER_VIEW', 'Посмотрели вакансии'), ('APPLICATION_STARTED', 'Начали анкету'), ('APPLICATION_COMPLETED', 'Заявки'), ('PARTNER_LINK_CLICK', 'Перешли к оформлению'), ('CONTRACT_CONFIRMED', 'Договоры'), ('REMINDER_SENT', 'Получили напоминания'), ('REMINDER_OPEN', 'Вернулись по кнопке'), ('REMINDER_CONVERTED', 'Заявок после follow-up')]


def ratio(a, b):
    return f'{100 * a / b:.1f}%' if b else '0.0%'


async def report(s, days=0, dimension=None):
    """Event-period unique users; explicit semantics, no double counting repeat visits."""
    query = select(Event)
    if days:
        since = now().replace(hour=0, minute=0, second=0, microsecond=0) if days == 1 else now() - timedelta(days=days)
        query = query.where(Event.at >= since)
    events = list((await s.scalars(query)).all())
    groups = defaultdict(lambda: defaultdict(set))
    for e in events:
        key = e.details.get(dimension, '') if dimension else 'Все'
        if dimension == 'source':
            key = {'avito': 'Авито', 'vk': 'VK', 'tg': 'Telegram', 'telegram': 'Telegram'}.get(key, 'Другие')
        groups[key or 'Не определено'][e.name].add(e.user_id)
    if not groups:
        groups['Все']
    rows = []
    for group, values in sorted(groups.items()):
        n = {name: len(values[name]) for name, _ in FUNNEL}
        rows.append([group, *[n[name] for name, _ in FUNNEL],
                     ratio(n['OFFER_VIEW'], n['BOT_OPEN']), ratio(n['APPLICATION_STARTED'], n['OFFER_VIEW']),
                     ratio(n['APPLICATION_COMPLETED'], n['APPLICATION_STARTED']), ratio(n['CONTRACT_CONFIRMED'], n['APPLICATION_COMPLETED']),
                     ratio(n['CONTRACT_CONFIRMED'], n['BOT_OPEN']),
                     ratio(len(values['APPLICATION_COMPLETED'] & values['REMINDER_SENT']), n['REMINDER_SENT'])])
    return rows


HEADERS = ['Разрез', *[title for _, title in FUNNEL], 'Вход → интерес', 'Интерес → начало анкеты', 'Начало → заявка', 'Заявка → договор', 'Вход → договор', 'Конверсия получателей follow-up']


async def summary(s, days=0):
    row = (await report(s, days))[0]
    drafts = list((await s.scalars(select(Application).where(Application.submitted_at.is_(None), Application.status == 'APPLICATION_STARTED'))).all())
    abandoned = sum((now() - (a.updated_at.replace(tzinfo=now().tzinfo) if a.updated_at.tzinfo is None else a.updated_at)).total_seconds() >= 86400 for a in drafts)
    sent_query = select(Event).where(Event.name == 'REMINDER_SENT')
    if days:
        since = now().replace(hour=0, minute=0, second=0, microsecond=0) if days == 1 else now() - timedelta(days=days)
        sent_query = sent_query.where(Event.at >= since)
    sent_count = len((await s.scalars(sent_query)).all())
    return '📊 ' + ('Всё время' if not days else f'За {days} дн. (UTC)') + '\n\n' + '\n'.join(f'{h}: {v}' for h, v in zip(HEADERS[1:], row[1:])) + f'\nНапоминаний отправлено (сообщений): {sent_count}\nНезавершённых анкет без активности 24 ч. сейчас: {abandoned}\n\nСчитаются уникальные пользователи по событиям периода.'

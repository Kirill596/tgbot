from aiogram import Router, F
from aiogram.types import CallbackQuery
from sqlalchemy import func, select
from app.analytics import report, summary
from app.config import settings
from app.database import Application, City, Offer, Reminder, User
from app.handlers import send
from app.keyboards import admin_menu, inline
from app.services import set_status, valid_url
from app.texts import LABELS

router = Router()
STATUS_LABELS = {'CONTRACT': '🟢 Договор заключён', 'WAITING': '🟡 В ожидании', 'REJECTED': '🔴 Отказ', 'NOT_RELEVANT': 'Не актуально', 'DUPLICATE': 'Дубликат'}


def lead_buttons(app):
    rows = [[(label, f'adm:status:{app.id}:{status}')] for status, label in STATUS_LABELS.items()]
    rows.append([('📞 Связаться', f'adm:contact:{app.id}')])
    rows.append([('✏️ Комментарий', f'adm:comment:{app.id}')])
    rows.append([('👨‍💻 Панель', 'admin')])
    return inline(*rows)


def lead_text(user, app):
    return f'Заявка #{app.id}\nСтатус: {app.status}\n\n' + '\n'.join(f'{LABELS.get(k, k)}: {v}' for k, v in app.answers.items()) + f'\nВакансия: {app.vacancy}\nПартнёр: {app.partner}\nИсточник: {app.source}\nКампания: {app.campaign}\nКомментарий: {app.comment or "—"}'


def application_list(apps, action, offset):
    if not apps:
        return 'Заявок в этом разделе пока нет.', [[('👨‍💻 Панель', 'admin')]]
    text = '🔥 Новые заявки' if action == 'new' else '👥 Все заявки'
    lines = [text, '']
    rows = []
    for app in apps:
        name = str(app.answers.get('name') or 'Без имени')[:40]
        city = str(app.answers.get('city') or 'город не указан')[:30]
        lines.append(f'#{app.id} · {name} · {city} · {app.status}')
        rows.append([(f'Открыть заявку #{app.id}', f'adm:lead:{app.id}')])
    rows.append([('⬅️ Назад', f'adm:{action}:{max(0, offset - 10)}'), ('Далее ➡️', f'adm:{action}:{offset + 10}')])
    rows.append([('👨‍💻 Панель', 'admin')])
    return '\n'.join(lines), rows


async def notify_application(s, user, app):
    send(s, settings().admin_id, '🔥 Новая заявка\n\n' + lead_text(user, app), lead_buttons(app))


@router.callback_query(F.data.startswith('adm:'))
async def admin_callback(q: CallbackQuery, s, user):
    # Acknowledge immediately: statistics and lead lookups may take longer than
    # Telegram's callback animation, especially after a database wake-up.
    await q.answer()
    if user.id != settings().admin_id:
        return
    parts = q.data.split(':')
    action = parts[1]
    if action == 'stats':
        if len(parts) == 2:
            send(s, user.id, '📊 Выбери период', inline([('Сегодня', 'adm:stats:1'), ('7 дней', 'adm:stats:7')], [('30 дней', 'adm:stats:30'), ('Всё время', 'adm:stats:0')]))
        else:
            days = int(parts[2])
            if days not in {0, 1, 7, 30}:
                raise ValueError('Неизвестный период')
            send(s, user.id, await summary(s, days), admin_menu())
    elif action in ('new', 'leads'):
        offset = max(0, int(parts[2]))
        query = select(Application).order_by(Application.id.desc()).offset(offset).limit(10)
        if action == 'new':
            query = query.where(Application.status == 'APPLICATION')
        apps = (await s.scalars(query)).all()
        body, rows = application_list(apps, action, offset)
        send(s, user.id, body, inline(*rows))
    elif action == 'lead':
        app = await s.get(Application, int(parts[2]))
        if not app:
            raise ValueError('Заявка не найдена.')
        owner = await s.get(User, app.user_id)
        send(s, user.id, lead_text(owner, app), lead_buttons(app))
    elif action == 'sources':
        dimension = parts[2] if len(parts) > 2 else 'source'
        if dimension not in {'source', 'city', 'campaign', 'offer', 'partner'}:
            return
        rows = await report(s, dimension=dimension)
        body = '📢 Аналитика по ' + dimension + '\n\n' + '\n'.join(f'{r[0]}: входы {r[1]}, интерес {r[2]}, заявки {r[4]}, договоры {r[6]}, вход → договор {r[14]}' for r in rows[:15])
        send(s, user.id, body, inline([('Источники', 'adm:sources:source'), ('Города', 'adm:sources:city')], [('Кампании', 'adm:sources:campaign'), ('Вакансии', 'adm:sources:offer')], [('Партнёры', 'adm:sources:partner'), ('👨‍💻 Панель', 'admin')]))
    elif action in ('offers', 'cities'):
        model = Offer if action == 'offers' else City
        rows = (await s.scalars(select(model).order_by(model.sort_order))).all()
        buttons, lines = [], []
        for row in rows:
            key = row.offer_id if action == 'offers' else row.city_id
            name = row.vacancy_name if action == 'offers' else row.city_name
            lines.append(f'{name} · {"активно" if row.active else "выключено"}')
            buttons.append([(f'Вкл./выкл. {name[:32]}', f'adm:toggle:{action}:{key}')])
            if action == 'offers':
                buttons.append([('🔗 Партнёрская ссылка', f'adm:offerurl:{key}')])
        buttons.append([('👨‍💻 Панель', 'admin')])
        send(s, user.id, ('💼 Вакансии' if action == 'offers' else '📍 Города') + '\n\n' + '\n'.join(lines), inline(*buttons))
    elif action == 'offerurl':
        offer = await s.get(Offer, parts[2])
        if not offer:
            raise ValueError('Вакансия не найдена.')
        user.ui = f'url:{offer.offer_id}'
        send(s, user.id, 'Пришлите HTTPS-ссылку оформления для этой вакансии. Для отмены нажмите Главное меню.', inline([('🏠 Главное меню', 'home')]))
    elif action == 'toggle':
        model = Offer if parts[2] == 'offers' else City
        row = await s.get(model, parts[3])
        if row:
            row.active = not row.active
            send(s, user.id, f'Активно: {row.active}. Сохранено в PostgreSQL.', admin_menu())
    elif action in ('status', 'contact', 'comment'):
        app = await s.get(Application, int(parts[2]))
        if not app:
            raise ValueError('Заявка не найдена.')
        owner = await s.scalar(select(User).where(User.id == app.user_id).with_for_update())
        if action == 'status':
            if len(parts) != 4 or parts[3] not in STATUS_LABELS:
                raise ValueError('Неизвестный статус')
            await set_status(s, owner, app, parts[3], user.id)
            send(s, user.id, lead_text(owner, app), lead_buttons(app))
        elif action == 'contact':
            # Telegram inline URL buttons do not support tel:; show copyable phone.
            send(s, user.id, f'Телефон: {app.answers.get("phone", "не указан")}\nTelegram: {"@" + owner.username if owner.username else "username не указан"}', lead_buttons(app))
        else:
            user.ui = f'comment:{app.id}'
            send(s, user.id, 'Напиши комментарий (до 1000 символов). Для отмены нажми Главное меню.')
    elif action == 'reminders':
        rows = (await s.execute(select(Reminder.status, func.count()).group_by(Reminder.status))).all()
        send(s, user.id, '🔔 Follow-up\n\n' + '\n'.join(f'{status}: {count}' for status, count in rows) + '\n\nunknown = исход отправки не подтверждён; автоматического повтора нет.', admin_menu())
    elif action == 'sync':
        send(s, user.id, 'Google Таблицы отключены. Все данные доступны здесь, в Telegram.', admin_menu())


@router.message(F.text)
async def admin_text(message, s, user):
    if user.id == settings().admin_id and user.ui.startswith('url:') and message.text != '🏠 Главное меню':
        url = message.text.strip()
        if not valid_url(url) or len(url) > 2000:
            raise ValueError('Пришлите корректную HTTPS-ссылку длиной до 2000 символов.')
        offer = await s.get(Offer, user.ui.split(':', 1)[1])
        if not offer:
            raise ValueError('Вакансия не найдена.')
        offer.partner_url = url
        user.ui = 'menu'
        send(s, user.id, 'Ссылка сохранена в PostgreSQL.', admin_menu())
        return
    if user.id == settings().admin_id and user.ui.startswith('comment:') and message.text != '🏠 Главное меню':
        app = await s.get(Application, int(user.ui.split(':')[1]))
        if app:
            app.comment = message.text[:1000]
        user.ui = 'menu'
        send(s, user.id, 'Комментарий сохранён.', admin_menu())
        return
    from app.handlers import message_handler
    await message_handler(message, s, user)

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import select
from app import texts
from app.config import settings
from app.database import Application, City, Offer, Outbox, Reminder, now
from app.keyboards import admin_menu, form_reply, home, inline, menu
from app.services import CLOSED, STEPS, event, phone, plan, source, start_application, submit, valid_url

router = Router()


def send(s, uid, text, markup=None, *, ui=True):
    """Save one replaceable user screen per update; keep explicit notifications separate."""
    raw_markup = markup.model_dump(exclude_none=True) if markup else None
    if ui:
        key = f'ui_outbox:{uid}'
        item = s.info.get(key)
        if item:
            item.text, item.markup = text[:4096], raw_markup
            return
        item = Outbox(chat_id=uid, text=text[:4096], markup=raw_markup, kind='ui')
        s.add(item)
        s.info[key] = item
        return
    s.add(Outbox(chat_id=uid, text=text[:4096], markup=raw_markup, kind='message'))


async def current(s, user):
    return await s.get(Application, user.current_application) if user.current_application else None


async def main_menu(s, user):
    user.ui = 'menu'
    body = texts.WELCOME + (f'\n\nМы запомнили твой город: {user.city}.' if user.city else '') + '\n\nВыбери раздел.'
    send(s, user.id, body, menu(user.id == settings().admin_id))
    app = await current(s, user)
    if app and not app.submitted_at and app.status not in CLOSED:
        send(s, user.id, texts.RETURN.format(vacancy=app.vacancy), inline([('➡️ Продолжить', 'resume')], [('🔄 Начать заново', 'restart:ask')]))


async def show_jobs(s, user, section='card'):
    offers = list((await s.scalars(select(Offer).where(Offer.active.is_(True)).order_by(Offer.sort_order))).all())
    send(s, user.id, '💼 Доступные вакансии' if offers else 'Сейчас нет активных вакансий. Загляни позже.', inline(*[[(o.vacancy_name, f'offer:{o.offer_id}:{section}')] for o in offers], [('🏠 Главное меню', 'home')]))


async def show_offer(s, user, offer, section='card'):
    user.viewed_offer = offer.offer_id
    if user.status == 'NEW':
        user.status = 'INTERESTED'
    event(s, user, 'OFFER_VIEW', offer=offer.offer_id, partner=offer.partner)
    body = offer.short_description
    if section == 'conditions':
        body = offer.conditions + '\n\n' + offer.requirements
        event(s, user, 'CONDITIONS_VIEW', offer=offer.offer_id, partner=offer.partner)
    elif section == 'schedule':
        body = offer.schedule
    elif section == 'faq':
        body = offer.full_description + '\n\n' + offer.faq + '\n\n' + texts.PROCESS
    elif section == 'cities':
        cities = (await s.scalars(select(City).where(City.active.is_(True)).order_by(City.sort_order))).all()
        body = '\n'.join(c.city_name for c in cities if not offer.cities or c.city_id in offer.cities) + '\n\nДругой город можно указать в анкете — доступность уточним.'
    send(s, user.id, offer.vacancy_name + '\n\n' + body, inline([('🚀 Оставить заявку', f'apply:{offer.offer_id}')], [('💰 Условия и выплаты', f'offer:{offer.offer_id}:conditions')], [('🕒 График', f'offer:{offer.offer_id}:schedule'), ('📍 Где можно работать', f'offer:{offer.offer_id}:cities')], [('❓ Вопросы', f'offer:{offer.offer_id}:faq')], [('🏠 Главное меню', 'home')]))
    await plan(s, user, await current(s, user))


async def show_step(s, user, app):
    if app.status in CLOSED:
        send(s, user.id, f'Статус заявки: {app.status}.', home())
        return
    if app.submitted_at:
        return await accepted(s, user, app)
    user.ui = 'form'
    if app.step == 'review':
        summary = '\n'.join(f'{label}: {app.answers.get(key, "—")}' for key, label in texts.LABELS.items())
        rows = [[('✅ Отправить заявку', f'submit:{app.id}')], [('✏️ Изменить', f'edit:{app.id}')], [('🏠 Главное меню', 'home')]]
        if valid_url(settings().privacy_url):
            rows.insert(0, [('Политика обработки данных', 'url:' + settings().privacy_url)])
        send(s, user.id, 'Проверь данные\n\n' + summary + f'\nВакансия: {app.vacancy}\n\n' + settings().consent_text, inline(*rows))
        return
    prompt = texts.QUESTIONS[app.step]
    rows = []
    if app.step == 'city':
        offer = await s.get(Offer, app.offer_id)
        cities = (await s.scalars(select(City).where(City.active.is_(True)).order_by(City.sort_order))).all()
        rows = [[(c.city_name, f'answer:{app.id}:city:{c.city_id}')] for c in cities if not offer.cities or c.city_id in offer.cities]
        rows.append([('Другой город', f'other:{app.id}')])
    elif app.step in ('readiness', 'experience'):
        choices = texts.READY if app.step == 'readiness' else texts.EXPERIENCE
        rows = [[(label, f'answer:{app.id}:{app.step}:{i}')] for i, label in enumerate(choices)]
    if rows:
        rows.append([('⬅️ Назад', 'form:back'), ('🏠 Главное меню', 'home')])
        send(s, user.id, prompt + '\n\nВыбери вариант:', inline(*rows))
    elif app.step == 'phone':
        send(s, user.id, prompt, form_reply(app.step))
    else:
        send(s, user.id, prompt, inline([('⬅️ Назад', 'form:back'), ('🏠 Главное меню', 'home')]))


async def save_answer(s, user, app, value):
    step = app.step
    value = value.strip()
    if step == 'age':
        if not value.isdigit() or not 14 <= int(value) <= 100:
            raise ValueError('Введи возраст числом от 14 до 100. Требования к возрасту зависят от вакансии.')
        value = int(value)
    elif step == 'phone':
        value = phone(value)
        event(s, user, 'PHONE_SHARED', app)
    elif step == 'readiness' and value not in texts.READY:
        raise ValueError('Выбери готовность кнопкой.')
    elif step == 'experience' and value not in texts.EXPERIENCE:
        raise ValueError('Выбери опыт кнопкой.')
    elif step not in ('readiness', 'experience') and not 2 <= len(value) <= 120:
        raise ValueError('Напиши от 2 до 120 символов.')
    app.answers = {**app.answers, step: value}
    if step == 'city':
        user.city = value
        city = await s.scalar(select(City).where(City.city_name == value, City.active.is_(True)))
        user.timezone = city.timezone if city else None
        event(s, user, 'CITY_SELECTED', app)
    event(s, user, 'APPLICATION_STEP', app, step=step)
    app.step = 'review' if app.editing else STEPS[STEPS.index(step) + 1]
    app.editing = False
    app.updated_at = now()
    await plan(s, user, app)
    await show_step(s, user, app)


async def accepted(s, user, app):
    send(s, user.id, texts.ACCEPTED + '\n\nМожно продолжить оформление или открыть помощь.', inline([('🚀 Перейти к оформлению', f'partner:{app.id}')], [('❓ Задать вопрос', 'faq'), ('🏠 Главное меню', 'home')]))


@router.message(CommandStart())
async def start(message: Message, s, user):
    payload = (message.text or '').split(maxsplit=1)
    if len(payload) == 2:
        src, campaign = source(payload[1])
        user.last_source, user.campaign = src, campaign
    event(s, user, 'BOT_OPEN')
    await main_menu(s, user)
    await plan(s, user, await current(s, user))


@router.callback_query(~F.data.startswith('adm:'))
async def callback(q: CallbackQuery, s, user):
    data = q.data or ''
    if data == 'admin':
        if user.id == settings().admin_id:
            send(s, user.id, '👨‍💻 Смена рядом', admin_menu())
        return
    if data == 'home':
        await main_menu(s, user)
    elif data == 'form:back':
        app = await current(s, user)
        if not app or app.submitted_at or app.status in CLOSED:
            raise ValueError(texts.STALE)
        app.step = STEPS[max(0, STEPS.index(app.step) - 1)]
        app.editing = False
        await show_step(s, user, app)
    elif data in ('jobs', 'conditions'):
        await show_jobs(s, user, 'conditions' if data == 'conditions' else 'card')
    elif data == 'cities':
        cities = (await s.scalars(select(City).where(City.active.is_(True)).order_by(City.sort_order))).all()
        send(s, user.id, '📍 Где ищешь подработку?', inline(*[[(c.city_name, f'city:{c.city_id}')] for c in cities], [('Другой город', 'city:other')], [('🏠 Главное меню', 'home')]))
    elif data.startswith('city:'):
        city = await s.get(City, data.split(':')[1])
        if city and city.active:
            user.city, user.timezone = city.city_name, city.timezone
            event(s, user, 'CITY_SELECTED')
            send(s, user.id, f'Мы запомнили твой город: {city.city_name}.', inline([('Продолжить', 'jobs')]))
        else:
            send(s, user.id, 'Другой город можно написать в анкете. Доступность уточним.', inline([('Продолжить', 'jobs')]))
    elif data == 'faq':
        rows = [[('Посмотреть вопросы по вакансии', 'jobs')], [('🏠 Главное меню', 'home')]]
        if valid_url(settings().support_url):
            rows.insert(0, [('❓ Написать в поддержку', 'url:' + settings().support_url)])
        send(s, user.id, texts.ABOUT + '\n\n' + texts.PROCESS, inline(*rows))
    elif data == 'disable':
        user.reminders_enabled = False
        event(s, user, 'REMINDER_DISABLED', await current(s, user))
        await plan(s, user, await current(s, user))
        send(s, user.id, '🔕 Автоматические напоминания отключены.', home())
    elif data.startswith('offer:'):
        _, oid, section = data.split(':')
        offer = await s.get(Offer, oid)
        if not offer or not offer.active:
            raise ValueError(texts.STALE)
        await show_offer(s, user, offer, section)
    elif data.startswith('apply:'):
        offer = await s.get(Offer, data.split(':')[1])
        if not offer or not offer.active:
            raise ValueError(texts.STALE)
        app = await start_application(s, user, offer)
        await show_step(s, user, app)
    elif data == 'resume':
        app = await current(s, user)
        if app:
            await show_step(s, user, app)
        else:
            await show_jobs(s, user)
    elif data.startswith('restart:'):
        app = await current(s, user)
        if not app or app.submitted_at or app.status in CLOSED:
            raise ValueError(texts.STALE)
        if data == 'restart:ask':
            send(s, user.id, 'Очистить ответы незавершённой анкеты?', inline([('Да, начать заново', f'restart:{app.id}')], [('Сохранить и продолжить', 'resume')]))
        elif data == f'restart:{app.id}':
            app.answers, app.step, app.editing = {}, 'name', False
            await show_step(s, user, app)
        else:
            raise ValueError(texts.STALE)
    elif data.startswith('rem:'):
        parts = data.split(':')
        reminder = await s.get(Reminder, int(parts[1]))
        if not reminder or reminder.user_id != user.id or reminder.status != 'sent':
            raise ValueError(texts.STALE)
        if not reminder.opened_at:
            reminder.opened_at, reminder.result = now(), 'opened'
            event(s, user, 'REMINDER_OPEN', await s.get(Application, reminder.application_id) if reminder.application_id else None, reminder_id=reminder.id)
        # Old reminder must never reset current progress or show a closed form.
        app = await current(s, user)
        if len(parts) > 2 and parts[2] == 'conditions' and user.viewed_offer:
            offer = await s.get(Offer, user.viewed_offer)
            if offer and offer.active:
                await show_offer(s, user, offer, 'conditions')
        elif app and app.status in CLOSED:
            await main_menu(s, user)
        elif app and app.clicked_at:
            send(s, user.id, texts.PROCESS, home())
        elif app:
            await show_step(s, user, app)
        else:
            await show_jobs(s, user)
    elif data.startswith(('answer:', 'other:', 'submit:', 'edit:', 'field:', 'partner:')):
        parts = data.split(':')
        app = await s.get(Application, int(parts[1]))
        if not app or app.user_id != user.id or app.id != user.current_application:
            raise ValueError(texts.STALE)
        if parts[0] == 'partner':
            if not app.submitted_at or app.status in CLOSED:
                raise ValueError(texts.STALE)
            offer = await s.get(Offer, app.offer_id)
            if not offer or not valid_url(offer.partner_url):
                send(s, user.id, 'Ссылка оформления пока не добавлена. Администратор уточнит следующий шаг.', home())
                return
            from app.services import signature
            cfg = settings()
            if cfg.public_url:
                url = f'{cfg.public_url.rstrip("/")}/go/{app.id}/{signature(app.id)}'
                send(s, user.id, 'Открыть страницу оформления партнёра:', inline([('🚀 Открыть оформление', 'url:' + url)]))
            else:
                # A Telegram URL button alone cannot prove a click. Do not emit a false click event.
                send(s, user.id, 'Открыть страницу оформления партнёра:', inline([('🚀 Открыть оформление', 'url:' + offer.partner_url)]))
            return
        if app.status in CLOSED:
            raise ValueError(texts.STALE)
        if app.submitted_at:
            return await accepted(s, user, app)
        if parts[0] == 'submit':
            if await submit(s, user, app):
                from app.admin import notify_application
                await notify_application(s, user, app)
            await accepted(s, user, app)
        elif parts[0] == 'edit':
            send(s, user.id, 'Какое поле изменить?', inline(*[[(label, f'field:{app.id}:{key}')] for key, label in texts.LABELS.items()], [('🏠 Главное меню', 'home')]))
        elif parts[0] == 'field' and parts[2] in STEPS[:-1]:
            app.step, app.editing = parts[2], True
            await show_step(s, user, app)
        elif parts[0] == 'other' and app.step == 'city':
            send(s, user.id, 'Напиши название города.', form_reply('city'))
        elif parts[0] == 'answer':
            if len(parts) != 4 or parts[2] != app.step:
                raise ValueError(texts.STALE)
            if app.step == 'city':
                city = await s.get(City, parts[3])
                offer = await s.get(Offer, app.offer_id)
                if not city or not city.active or (offer.cities and city.city_id not in offer.cities):
                    raise ValueError(texts.STALE)
                value = city.city_name
            elif app.step in ('readiness', 'experience'):
                choices = texts.READY if app.step == 'readiness' else texts.EXPERIENCE
                if not parts[3].isdigit() or int(parts[3]) >= len(choices):
                    raise ValueError(texts.STALE)
                value = choices[int(parts[3])]
            else:
                raise ValueError(texts.STALE)
            await save_answer(s, user, app, value)
    else:
        send(s, user.id, texts.STALE, home())


@router.message()
async def message_handler(message: Message, s, user):
    if message.text == '🏠 Главное меню':
        return await main_menu(s, user)
    app = await current(s, user)
    if user.ui != 'form' or not app or app.submitted_at or app.status in CLOSED:
        return await main_menu(s, user)
    if message.text == '⬅️ Назад':
        if app.editing:
            app.step, app.editing = 'review', False
        else:
            app.step = STEPS[max(0, STEPS.index(app.step) - 1)]
        return await show_step(s, user, app)
    if app.step == 'review':
        return await show_step(s, user, app)
    if message.contact:
        if app.step != 'phone' or message.contact.user_id != user.id:
            raise ValueError('Отправь свой номер кнопкой или введи его вручную.')
        value = message.contact.phone_number
        if not value.startswith('+'):
            value = '+' + value
    else:
        value = message.text or ''
    await save_answer(s, user, app, value)

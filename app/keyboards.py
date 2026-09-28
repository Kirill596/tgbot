from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def inline(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, **({'url': value[4:]} if value.startswith('url:') else {'callback_data': value}))
        for label, value in row] for row in rows])


def menu(admin=False):
    rows = [[('🔎 Найти подработку', 'jobs')], [('💼 Вакансии', 'jobs'), ('💰 Условия', 'conditions')], [('📍 Города', 'cities'), ('❓ Частые вопросы', 'faq')], [('🔕 Не напоминать', 'disable')]]
    if admin:
        rows.append([('👨‍💻 Панель администратора', 'admin')])
    return inline(*rows)


def home():
    return inline([('🏠 Главное меню', 'home')])


def form_reply(step):
    rows = []
    if step == 'phone':
        rows.append([KeyboardButton(text='📱 Отправить номер', request_contact=True)])
    rows += [[KeyboardButton(text='⬅️ Назад'), KeyboardButton(text='🏠 Главное меню')]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_menu():
    return inline([('📊 Статистика', 'adm:stats'), ('🔥 Новые заявки', 'adm:new:0')], [('👥 Все заявки', 'adm:leads:0'), ('📢 Источники', 'adm:sources')], [('💼 Вакансии', 'adm:offers'), ('📍 Города', 'adm:cities')], [('🔄 Статусы', 'adm:leads:0'), ('🔔 Follow-up', 'adm:reminders')], [('🏠 Главное меню', 'home')])

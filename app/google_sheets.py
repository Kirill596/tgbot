import asyncio
import json
from pathlib import Path
import structlog
from sqlalchemy import select
from app.analytics import HEADERS, report, summary
from app.config import settings
from app.database import Application, City, Event, Offer, Reminder, Session, User
from app.catalog import OFFER_FIELDS as OFFER_FIELDS, CITY_FIELDS as CITY_FIELDS, parse_catalog as parse_catalog

log = structlog.get_logger()
sync_lock = asyncio.Lock()
LEAD_FIELDS = ['Lead ID', 'User ID', 'Telegram ID', 'Username', 'Дата первого входа', 'Дата последней активности', 'Имя', 'Возраст', 'Телефон', 'Город', 'Район', 'Партнёр', 'Вакансия', 'Offer ID', 'First Source', 'Last Source', 'Campaign', 'Статус', 'Этап анкеты', 'Готов начать', 'Опыт', 'Partner Link Clicked', 'Reminders Enabled', 'Last Reminder', 'Дата заявки', 'Дата изменения статуса', 'Комментарий администратора']


def cell(v):
    if v is None:
        return ''
    if hasattr(v, 'isoformat'):
        return v.isoformat()
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


def client():
    # Optional adapter: imported only by the explicit CLI sync command.
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    raw = settings().google_credentials.get_secret_value()
    info = json.loads(raw) if raw.lstrip().startswith('{') else json.loads(Path(raw).read_text(encoding='utf-8'))
    credentials = Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=30))
    return build('sheets', 'v4', http=http, cache_discovery=False).spreadsheets()


def ensure_and_read():
    api, sid = client(), settings().google_sheet_id
    existing = {x['properties']['title'] for x in api.get(spreadsheetId=sid).execute()['sheets']}
    names = ['Лиды', 'Аналитика', 'Офферы', 'Города', 'События', 'Напоминания', 'Источники', 'По городам', 'Кампании', 'Вакансии', 'Партнёры']
    missing = [x for x in names if x not in existing]
    if missing:
        api.batchUpdate(spreadsheetId=sid, body={'requests': [{'addSheet': {'properties': {'title': x}}} for x in missing]}).execute()
    values = api.values().batchGet(spreadsheetId=sid, ranges=["'Офферы'!A:M", "'Города'!A:E"]).execute()['valueRanges']
    return [v.get('values', []) for v in values]


def write_sheets(data):
    api, sid = client(), settings().google_sheet_id
    properties = api.get(spreadsheetId=sid, fields='sheets.properties').execute()['sheets']
    grids = {sheet['properties']['title']: sheet['properties'] for sheet in properties}
    resize = []
    for title, rows in data.items():
        props = grids[title]
        grid = props['gridProperties']
        needed_rows = max(grid['rowCount'], len(rows) + 1)
        needed_cols = max(grid['columnCount'], 52)
        if needed_rows != grid['rowCount'] or needed_cols != grid['columnCount']:
            resize.append({'updateSheetProperties': {'properties': {'sheetId': props['sheetId'], 'gridProperties': {'rowCount': needed_rows, 'columnCount': needed_cols}}, 'fields': 'gridProperties(rowCount,columnCount)'}})
    if resize:
        api.batchUpdate(spreadsheetId=sid, body={'requests': resize}).execute()
    # RAW prevents candidate-controlled values from becoming spreadsheet formulas.
    api.values().batchUpdate(spreadsheetId=sid, body={'valueInputOption': 'RAW', 'data': [{'range': f"'{title}'!A1", 'values': rows} for title, rows in data.items()]}).execute()
    # Clear stale tails only after the fresh snapshot was successfully written.
    api.values().batchClear(spreadsheetId=sid, body={'ranges': [f"'{title}'!A{len(rows) + 1}:AZ" for title, rows in data.items()]}).execute()


async def sync():
    if not settings().google_sheet_id or not settings().google_credentials.get_secret_value():
        return 'Google Sheets не настроен: добавьте GOOGLE_SHEET_ID и GOOGLE_CREDENTIALS.'
    if sync_lock.locked():
        return 'Синхронизация уже выполняется.'
    async with sync_lock:
        try:
            offer_rows, city_rows = await asyncio.to_thread(ensure_and_read)
            offers_in = parse_catalog(offer_rows, OFFER_FIELDS)
            cities_in = parse_catalog(city_rows, CITY_FIELDS)
            async with Session.begin() as s:
                known_cities = {c.city_id for c in (await s.scalars(select(City))).all()} | {c['city_id'] for c in cities_in}
                if any(set(o['cities']) - known_cities for o in offers_in):
                    raise ValueError('Unknown city ID in offer')
                for model, records, key in ((City, cities_in, 'city_id'), (Offer, offers_in, 'offer_id')):
                    for row in records:
                        existing = await s.get(model, row[key])
                        if existing:
                            for name, value in row.items():
                                setattr(existing, name, value)
                        else:
                            s.add(model(**row))
                await s.flush()
                users = (await s.scalars(select(User).order_by(User.id))).all()
                apps = (await s.scalars(select(Application).order_by(Application.id))).all()
                leads = [LEAD_FIELDS]
                for u in users:
                    for a in [a for a in apps if a.user_id == u.id] or [None]:
                        ans = a.answers if a else {}
                        leads.append([cell(v) for v in [f'A{a.id}' if a else f'U{u.id}', u.id, u.id, u.username,
                            u.first_visit, u.last_visit, ans.get('name'), ans.get('age'), ans.get('phone'), ans.get('city', u.city), ans.get('district'),
                            a.partner if a else '', a.vacancy if a else '', a.offer_id if a else '', u.first_source, u.last_source,
                            a.campaign if a else u.campaign, a.status if a else u.status, a.step if a else '', ans.get('readiness'), ans.get('experience'),
                            bool(a and a.clicked_at), u.reminders_enabled, u.last_reminder, a.submitted_at if a else '', a.updated_at if a else '', a.comment if a else '']])
                data = {'Лиды': leads, 'Аналитика': [HEADERS, *(await report(s)), ['Методика', 'Уникальные пользователи по событиям за всё время. REMINDER_OPEN = нажатие кнопки, не чтение сообщения.'], ['Незавершённые анкеты', (await summary(s)).split('Незавершённых анкет')[1].split('\n')[0]]]}
                for title, dim in [('Источники', 'source'), ('По городам', 'city'), ('Кампании', 'campaign'), ('Вакансии', 'offer'), ('Партнёры', 'partner')]:
                    data[title] = [HEADERS, *(await report(s, dimension=dim))]
                for title, model, fields, original in [('Офферы', Offer, OFFER_FIELDS, offer_rows), ('Города', City, CITY_FIELDS, city_rows)]:
                    # Catalog sheets belong to administrator; seed only truly empty sheets.
                    if not original:
                        records = (await s.scalars(select(model).order_by(model.sort_order))).all()
                        data[title] = [fields, *[[cell(getattr(r, f)) for f in fields] for r in records]]
                events = (await s.scalars(select(Event).order_by(Event.id))).all()
                data['События'] = [['ID', 'User ID', 'Application ID', 'Событие', 'Время', 'Параметры'], *[[cell(v) for v in [e.id, e.user_id, e.application_id, e.name, e.at, e.details]] for e in events]]
                reminders = (await s.scalars(select(Reminder).order_by(Reminder.id))).all()
                fields = ['id', 'user_id', 'application_id', 'reminder_type', 'stage', 'scheduled_at', 'sent_at', 'opened_at', 'converted_at', 'cancelled_at', 'status', 'result']
                data['Напоминания'] = [fields, *[[cell(getattr(r, f)) for f in fields] for r in reminders]]
            await asyncio.to_thread(write_sheets, data)
            log.info('sheets_synced')
            return '✅ CRM синхронизирована.'
        except Exception as exc:
            log.error('sheets_failed', error_type=type(exc).__name__)
            return '❌ CRM не синхронизирована. Проверьте ключ, доступ к таблице и формат каталогов. Данные в PostgreSQL сохранены; следующая синхронизация повторит выгрузку.'

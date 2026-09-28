"""Catalog validation independent of any external integration."""
import json
import re
from zoneinfo import ZoneInfo
from app.services import valid_url

OFFER_FIELDS = ['offer_id', 'active', 'partner', 'vacancy_name', 'short_description', 'full_description', 'cities', 'requirements', 'conditions', 'partner_url', 'sort_order', 'schedule', 'faq']
CITY_FIELDS = ['city_id', 'city_name', 'active', 'sort_order', 'timezone']


def parse_catalog(rows, fields):
    if not rows or len(rows) == 1:
        return []
    if rows[0] != fields:
        raise ValueError('Catalog headers differ from expected columns')
    parsed, seen = [], set()
    for row in rows[1:]:
        if not row or not str(row[0]).strip():
            continue
        value = dict(zip(fields, [*row, *([''] * (len(fields) - len(row)))]))
        key = value[fields[0]]
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,24}', key) or key in seen:
            raise ValueError('Catalog ID must be unique, 1..24 ASCII characters')
        seen.add(key)
        active = str(value['active']).lower()
        if active not in {'true', 'false', '1', '0', 'да', 'нет'}:
            raise ValueError('active must be TRUE or FALSE')
        value['active'] = active in {'true', '1', 'да'}
        value['sort_order'] = int(value['sort_order'] or 0)
        if 'cities' in value:
            cities = value['cities']
            value['cities'] = json.loads(cities) if cities.strip().startswith('[') else [x.strip() for x in cities.split(',') if x.strip()]
            if not isinstance(value['cities'], list) or any(not isinstance(x, str) for x in value['cities']):
                raise ValueError('cities must be city IDs')
            if value['partner_url'] and not valid_url(value['partner_url']):
                raise ValueError('partner_url must be HTTPS')
            for k in ('partner', 'vacancy_name', 'short_description'):
                if not value[k]:
                    raise ValueError('Required offer field is empty')
            if len(value['partner']) > 160 or len(value['vacancy_name']) > 160:
                raise ValueError('Offer title too long')
            if any(len(str(value[k])) > 1500 for k in ('short_description', 'full_description', 'requirements', 'conditions', 'schedule', 'faq')):
                raise ValueError('Offer text too long (1500 max per field)')
        else:
            if not value['city_name'] or len(value['city_name']) > 120:
                raise ValueError('Invalid city name')
            value['timezone'] = value['timezone'] or 'Europe/Moscow'
            ZoneInfo(value['timezone'])
        parsed.append(value)
    return parsed



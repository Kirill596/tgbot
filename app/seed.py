import json
from pathlib import Path
from app.database import City, Offer, Session
from app.catalog import CITY_FIELDS, OFFER_FIELDS, parse_catalog


async def seed(update=False):
    data = json.loads((Path(__file__).parent.parent / 'config/catalog.json').read_text(encoding='utf-8'))
    def validated(rows, fields):
        table = [fields]
        for row in rows:
            values = {**row}
            if 'cities' in fields:
                values['cities'] = json.dumps(values.get('cities', []))
            values.setdefault('active', True)
            table.append([values.get(f, '') for f in fields])
        return parse_catalog(table, fields)
    cities = validated(data['cities'], CITY_FIELDS)
    offers = validated(data['offers'], OFFER_FIELDS)
    known = {c['city_id'] for c in cities}
    if any(set(o['cities']) - known for o in offers):
        raise ValueError('В оффере указан city_id, отсутствующий в config/catalog.json')
    async with Session.begin() as s:
        for model, rows, key in ((City, cities, 'city_id'), (Offer, offers, 'offer_id')):
            for row in rows:
                existing = await s.get(model, row[key])
                if existing is None:
                    s.add(model(**row))
                elif update:
                    for name, value in row.items():
                        setattr(existing, name, value)

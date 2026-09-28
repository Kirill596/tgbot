import os
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.database import Base, City, Offer, User

@pytest.fixture
async def sessions(tmp_path):
    url = os.environ.get('TEST_DATABASE_URL', f'sqlite+aiosqlite:///{tmp_path}/test.db')
    if 'postgresql' in url and '/smena_test' not in url:
        raise RuntimeError('PostgreSQL tests require a dedicated database named smena_test')
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        if 'postgresql' in url:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as s:
        s.add(User(id=10, first_source='avito', last_source='avito', campaign='avito_moscow_01', timezone='Europe/Moscow', status='NEW', reminders_enabled=True, unanswered=0))
        s.add(City(city_id='moscow', city_name='Москва', timezone='Europe/Moscow'))
        for oid in ('first', 'second'):
            s.add(Offer(offer_id=oid, partner='Partner', vacancy_name='Курьер', short_description='Описание', full_description='Описание', cities=[], active=True))
    yield factory
    await engine.dispose()



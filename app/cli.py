"""Operational commands; no bot token required for DB reports/migrations."""
import argparse
import asyncio
from datetime import timedelta
from sqlalchemy import select
from app.database import Reminder, Session, User, engine, now
from app.seed import seed


async def run(args):
    if args.command == 'seed':
        await seed()
        print('Каталоги созданы (существующие записи сохранены).')
    elif args.command == 'sync':
        from app.google_sheets import sync
        print(await sync())
    elif args.command == 'catalog':
        await seed(update=True)
        print('Города и вакансии обновлены в PostgreSQL из config/catalog.json.')
    elif args.command == 'test-reminder':
        async with Session.begin() as s:
            user = await s.get(User, args.user)
            if not user:
                raise SystemExit('Сначала откройте бота этим аккаунтом.')
            reminder = await s.scalar(select(Reminder).where(Reminder.user_id == args.user, Reminder.status == 'pending').order_by(Reminder.scheduled_at))
            if not reminder:
                raise SystemExit('Нет ожидающего напоминания. Пройдите нужный этап новым тестовым пользователем.')
            reminder.scheduled_at = now() - timedelta(seconds=1)
            print('Напоминание назначено сейчас. Дневное окно и запрет напоминаний сохраняются. Дождитесь очередного tick.')
    await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('seed')
    sub.add_parser('sync')
    sub.add_parser('catalog')
    test = sub.add_parser('test-reminder')
    test.add_argument('--user', type=int, required=True)
    asyncio.run(run(parser.parse_args()))

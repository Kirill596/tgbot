from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from app.config import settings


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_source: Mapped[str] = mapped_column(String(64), default='direct')
    last_source: Mapped[str] = mapped_column(String(64), default='direct')
    campaign: Mapped[str] = mapped_column(String(64), default='direct')
    first_visit: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_visit: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    city: Mapped[str] = mapped_column(String(120), default='')
    timezone: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32), default='NEW')
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_reminder: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unanswered: Mapped[int] = mapped_column(Integer, default=0)
    current_application: Mapped[int | None] = mapped_column(Integer)
    viewed_offer: Mapped[str | None] = mapped_column(String(40))
    ui: Mapped[str] = mapped_column(String(32), default='menu')
    ui_message_id: Mapped[int | None] = mapped_column(Integer)


class City(Base):
    __tablename__ = 'cities'
    city_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    city_name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    timezone: Mapped[str] = mapped_column(String(80), default='Europe/Moscow')


class Offer(Base):
    __tablename__ = 'offers'
    offer_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    partner: Mapped[str] = mapped_column(String(160))
    vacancy_name: Mapped[str] = mapped_column(String(160))
    short_description: Mapped[str] = mapped_column(Text)
    full_description: Mapped[str] = mapped_column(Text)
    cities: Mapped[list] = mapped_column(JSON, default=list)
    requirements: Mapped[str] = mapped_column(Text, default='')
    conditions: Mapped[str] = mapped_column(Text, default='')
    schedule: Mapped[str] = mapped_column(Text, default='')
    faq: Mapped[str] = mapped_column(Text, default='')
    partner_url: Mapped[str] = mapped_column(Text, default='')
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Application(Base):
    __tablename__ = 'applications'
    __table_args__ = (UniqueConstraint('user_id', 'offer_id'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    offer_id: Mapped[str] = mapped_column(ForeignKey('offers.offer_id'))
    partner: Mapped[str] = mapped_column(String(160))
    vacancy: Mapped[str] = mapped_column(String(160))
    source: Mapped[str] = mapped_column(String(64))
    campaign: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default='APPLICATION_STARTED')
    step: Mapped[str] = mapped_column(String(32), default='name')
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_text: Mapped[str] = mapped_column(Text, default='')
    comment: Mapped[str] = mapped_column(Text, default='')
    editing: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Base):
    __tablename__ = 'events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey('applications.id'))
    name: Mapped[str] = mapped_column(String(40), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Reminder(Base):
    __tablename__ = 'reminders'
    __table_args__ = (UniqueConstraint('dedup_key'), Index('ix_reminders_due', 'status', 'scheduled_at'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(150))
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    application_id: Mapped[int | None] = mapped_column(ForeignKey('applications.id'))
    reminder_type: Mapped[str] = mapped_column(String(40))
    stage: Mapped[str] = mapped_column(String(32))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default='pending')
    result: Mapped[str] = mapped_column(String(64), default='')


class StatusHistory(Base):
    __tablename__ = 'status_history'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey('applications.id'))
    old_status: Mapped[str] = mapped_column(String(32))
    new_status: Mapped[str] = mapped_column(String(32))
    actor: Mapped[int] = mapped_column(BigInteger)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UpdateReceipt(Base):
    __tablename__ = 'update_receipts'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Outbox(Base):
    __tablename__ = 'outbox'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    markup: Mapped[dict | None] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(String(16), default='message')
    status: Mapped[str] = mapped_column(String(24), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def make_engine(url=None):
    value = url or settings().database_url.get_secret_value()
    value = value.replace('postgres://', 'postgresql+asyncpg://', 1)
    if value.startswith('postgresql://'):
        value = value.replace('postgresql://', 'postgresql+asyncpg://', 1)
    # asyncpg calls the SSL parameter ssl, not libpq's sslmode.
    value = value.replace('sslmode=', 'ssl=')
    return create_async_engine(value, pool_pre_ping=True)


engine = make_engine()
Session = async_sessionmaker(engine, expire_on_commit=False)

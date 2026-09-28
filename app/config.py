from functools import lru_cache
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    bot_token: SecretStr = SecretStr('')
    admin_id: int = 1132113524
    database_url: SecretStr = SecretStr('postgresql+asyncpg://smena:smena@localhost:5432/smena')
    google_sheet_id: str = ''
    google_credentials: SecretStr = SecretStr('')
    mode: str = 'polling'
    public_url: str = ''
    webhook_secret: SecretStr = SecretStr('')
    cron_secret: SecretStr = SecretStr('')
    link_secret: SecretStr = SecretStr('')
    port: int = 8080
    scheduler_mode: str = 'internal'
    default_timezone: str = 'Europe/Moscow'
    reminder_hours_new: float = 4
    reminder_hours_interested: float = 18
    reminder_hours_draft: float = 6
    reminder_hours_second: float = 36
    reminder_hours_application: float = 12
    reminder_hours_clicked: float = 36
    reminder_min_gap_hours: float = 12
    reminder_max_unanswered: int = 3
    consent_text: str = 'Контактные данные используются сервисом «Смена рядом» для обработки заявки на выбранную вакансию. Отправляя заявку, вы соглашаетесь с этой обработкой.'
    privacy_url: str = ''
    support_url: str = ''

    @field_validator('admin_id')
    @classmethod
    def owner_only(cls, v):
        if v != 1132113524:
            raise ValueError('Для этого проекта ADMIN_ID должен быть 1132113524')
        return v

    @field_validator('reminder_hours_new', 'reminder_hours_interested', 'reminder_hours_draft', 'reminder_hours_second', 'reminder_hours_application', 'reminder_hours_clicked', 'reminder_min_gap_hours')
    @classmethod
    def positive_delay(cls, v):
        if v <= 0:
            raise ValueError('Reminder delays must be positive')
        return v

    @field_validator('reminder_max_unanswered')
    @classmethod
    def limited_reminders(cls, v):
        if not 1 <= v <= 5:
            raise ValueError('REMINDER_MAX_UNANSWERED must be 1..5')
        return v

    @field_validator('mode')
    @classmethod
    def mode_valid(cls, v):
        if v not in {'polling', 'webhook'}:
            raise ValueError('MODE must be polling or webhook')
        return v

    @model_validator(mode='after')
    def validate_web(self):
        if self.mode == 'webhook':
            if not self.public_url.startswith('https://'):
                raise ValueError('PUBLIC_URL must be https://...')
            for key in ('webhook_secret', 'cron_secret', 'link_secret'):
                if len(getattr(self, key).get_secret_value()) < 32:
                    raise ValueError(f'{key} needs at least 32 characters')
        if self.scheduler_mode not in {'internal', 'external'}:
            raise ValueError('SCHEDULER_MODE must be internal or external')
        return self


@lru_cache
def settings():
    return Settings()

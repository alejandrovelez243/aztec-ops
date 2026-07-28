"""Project settings.

One settings module for every environment. What differs between local, test and
production is *data*, not code, so it arrives through the environment and is validated
here by :class:`Settings` rather than by a family of modules that drift apart.

The rule this file follows: a value that changes per environment is a field on
``Settings``; a value that is the same everywhere is a plain module constant below.
Anything genuinely conditional keys off ``ENVIRONMENT`` in one visible place.
"""

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import django_stubs_ext
from celery.schedules import crontab
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# Makes Django's generic classes subscriptable at runtime (QuerySet[Project],
# TabularInline[WorkflowState, Workflow]). Without it those annotations are only valid inside
# TYPE_CHECKING, and the admin fails to import with "type is not subscriptable".
django_stubs_ext.monkeypatch()


class Settings(BaseSettings):
    """Environment-provided configuration, validated at import time.

    Validating here means a missing or malformed variable fails at startup with a named
    error, instead of surfacing later as a confusing runtime failure in a request.
    """

    model_config = SettingsConfigDict(
        env_file=BASE_DIR.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["local", "test", "production"] = Field(
        default="local", alias="ENVIRONMENT"
    )
    secret_key: str = Field(default="dev-only-not-a-secret", alias="DJANGO_SECRET_KEY")
    debug: bool = Field(default=True, alias="DJANGO_DEBUG")
    allowed_hosts: list[str] = Field(default=["*"], alias="DJANGO_ALLOWED_HOSTS")

    database_url: str = Field(
        default="postgres://aztec:aztec@localhost:5432/aztec", alias="DATABASE_URL"
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    cors_origins: list[str] = Field(default=["http://localhost:4321"], alias="CORS_ALLOWED_ORIGINS")

    #: How often the ticker emits ``clock.ticked``. This is the upper bound on how stale a
    #: time-derived score can be (ARCHITECTURE §4.2).
    ticker_interval_seconds: int = Field(default=300, alias="TICKER_INTERVAL_SECONDS")

    #: A project with no recorded activity for this many days trips ``IS_STALE``.
    staleness_threshold_days: int = Field(default=14, alias="STALENESS_THRESHOLD_DAYS")

    #: How many times a consumer retries an event before it goes to the dead letter stream.
    event_max_attempts: int = Field(default=5, alias="EVENT_MAX_ATTEMPTS")

    # Plain properties, not ``computed_field``: nothing serializes this object — every reader is
    # Django module code below, reading the attribute — so the only thing the decorator would add
    # is a key in a ``model_dump`` that is never called, and it hides the property from mypy.
    @property
    def is_production(self) -> bool:
        """Whether hardened defaults apply."""
        return self.environment == "production"

    @property
    def database(self) -> dict[str, object]:
        """The Django ``DATABASES['default']`` entry, parsed from ``DATABASE_URL``.

        One URL is easier to pass through Compose and a deploy target than six separate
        variables that can disagree with each other.
        """
        url = urlparse(self.database_url)
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": url.path.lstrip("/"),
            "USER": url.username,
            "PASSWORD": url.password,
            "HOST": url.hostname,
            "PORT": url.port or 5432,
            "CONN_MAX_AGE": 60 if self.is_production else 0,
        }


settings = Settings()

# --- Django ------------------------------------------------------------------------

SECRET_KEY = settings.secret_key
DEBUG = settings.debug and not settings.is_production
ALLOWED_HOSTS = settings.allowed_hosts
DATABASES = {"default": settings.database}

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Required by the GIN index on ProjectSnapshot's risk-flag array (DATA_MODEL §8).
    "django.contrib.postgres",
    # Identity first: AUTH_USER_MODEL is referenced by everything that follows.
    "apps.accounts",
    # Bounded contexts, in dependency order.
    "apps.catalog",
    "apps.workflow",
    "apps.portfolio",
    "apps.work",
    "apps.activity",
    "apps.events",
    "apps.prioritization",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

#: Set on day one. Changing it after the first migration means hand-written surgery
#: across every table holding a user foreign key.
AUTH_USER_MODEL = "accounts.User"

# --- Project -----------------------------------------------------------------------

REDIS_URL = settings.redis_url
CORS_ALLOWED_ORIGINS = settings.cors_origins
TICKER_INTERVAL_SECONDS = settings.ticker_interval_seconds
STALENESS_THRESHOLD_DAYS = settings.staleness_threshold_days
EVENT_MAX_ATTEMPTS = settings.event_max_attempts

#: Redis Streams topology. Names are configuration, never literals scattered through code.
EVENT_STREAM = "aztec.events"
EVENT_DLQ_STREAM = "aztec.events.dlq"
EVENT_SSE_CHANNEL = "aztec.sse"

# --- Celery ------------------------------------------------------------------------
# Celery is the scheduler, not the bus. Its only tasks write clock.ticked to the outbox; the
# relay publishes it to Redis Streams like any other event. See docs/adr/0009.

CELERY_BROKER_URL = settings.redis_url
CELERY_RESULT_BACKEND = settings.redis_url
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True

#: A tick is worthless once the next one is due, so an unacknowledged one is not worth redelivering.
CELERY_TASK_ACKS_LATE = False
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

CELERY_BEAT_SCHEDULE = {
    "emit-interval-tick": {
        "task": "events.emit_interval_tick",
        "schedule": float(settings.ticker_interval_seconds),
    },
    "emit-day-boundary-tick": {
        "task": "events.emit_day_boundary_tick",
        # Local midnight. Overdue and days-open change here, not on the interval grid.
        "schedule": crontab(hour=0, minute=0),
    },
}

# --- Environment-conditional --------------------------------------------------------
# The only branching in this file. If this block grows past a handful of entries, that is
# the signal to reconsider, not to add a second settings module.

if settings.is_production:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31_536_000

if settings.environment == "test":
    # Test users are created constantly and nobody attacks them.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

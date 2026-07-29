"""Project settings.

One settings module for every environment. What differs between local, test and
production is *data*, not code, so it arrives through the environment and is validated
here by :class:`Settings` rather than by a family of modules that drift apart.

The rule this file follows: a value that changes per environment is a field on
``Settings``; a value that is the same everywhere is a plain module constant below.
Anything genuinely conditional keys off ``ENVIRONMENT`` in one visible place.
"""

from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import django_stubs_ext
from celery.schedules import crontab
from corsheaders.defaults import default_headers
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
    #: Long enough for HS256 (PyJWT warns below 32 bytes) and obviously not a secret, so a deployment
    #: that forgot ``DJANGO_SECRET_KEY`` is embarrassing rather than subtly weak.
    secret_key: str = Field(
        default="dev-only-not-a-secret-do-not-use-this-in-production",
        alias="DJANGO_SECRET_KEY",
    )
    debug: bool = Field(default=True, alias="DJANGO_DEBUG")
    allowed_hosts: list[str] = Field(default=["*"], alias="DJANGO_ALLOWED_HOSTS")

    database_url: str = Field(
        default="postgres://aztec:aztec@localhost:5432/aztec", alias="DATABASE_URL"
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    #: Appended to the test database's name, so two suites running at once do not share one
    #: database and drop it from under each other. Empty in the ordinary single-run case.
    test_db_suffix: str = Field(default="", alias="TEST_DB_SUFFIX")
    cors_origins: list[str] = Field(default=["http://localhost:4321"], alias="CORS_ALLOWED_ORIGINS")

    #: How often the ticker emits ``clock.ticked``. This is the upper bound on how stale a
    #: time-derived score can be (ARCHITECTURE §4.2).
    ticker_interval_seconds: int = Field(default=300, alias="TICKER_INTERVAL_SECONDS")

    #: A project with no recorded activity for this many days trips ``IS_STALE``.
    staleness_threshold_days: int = Field(default=14, alias="STALENESS_THRESHOLD_DAYS")

    #: How many times a handler is attempted for one event before the outbox row is dead
    #: lettered. Counted per handler, so one poisoned reactor does not retire the others.
    event_max_attempts: int = Field(default=5, alias="EVENT_MAX_ATTEMPTS")

    #: How often Celery Beat sweeps the outbox. This is the *fallback* latency, not the normal
    #: one: ``enqueue_event`` kicks the drain on commit, so the sweep only matters when the broker
    #: was unreachable at that moment.
    event_drain_interval_seconds: float = Field(default=5.0, alias="EVENT_DRAIN_INTERVAL_SECONDS")

    #: Rows claimed per drain pass. Large enough that a burst drains in a few round trips, small
    #: enough that one worker never holds the whole backlog locked.
    event_drain_batch_size: int = Field(default=100, alias="EVENT_DRAIN_BATCH_SIZE")

    # Plain properties, not ``computed_field``: nothing serializes this object — every reader is
    # Django module code below, reading the attribute — so the only thing the decorator would add
    # --- Seed credentials -----------------------------------------------------------------
    # Read by ``manage.py seed``. Every one defaults to empty ON PURPOSE: a default that works is
    # still a hardcoded credential, only one everybody knows, and this repository is public.
    # Absent means "nobody can sign in yet", which is a safe failure. "admin/admin" is not.

    #: Applied to every seeded team member. Empty leaves them with an unusable password.
    seed_user_password: str = Field(default="", alias="SEED_USER_PASSWORD")

    #: Django's own convention — the same variables ``createsuperuser --noinput`` reads.
    superuser_username: str = Field(default="", alias="DJANGO_SUPERUSER_USERNAME")
    superuser_password: str = Field(default="", alias="DJANGO_SUPERUSER_PASSWORD")
    superuser_email: str = Field(default="", alias="DJANGO_SUPERUSER_EMAIL")

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

        ``TEST['NAME']`` is a suffix Django would otherwise derive as ``test_<name>`` for
        everybody, which makes two simultaneous runs share one database: the second run's
        setup drops the first run's database out from under it, and the failure arrives as
        ``database "test_aztec" does not exist`` on a test that has nothing wrong with it.
        Setting ``TEST_DB_SUFFIX`` gives a run its own database, so a second suite (a
        parallel agent, a colleague on the same host, two shells) cannot collide with it.
        Empty by default, which keeps the ordinary single-run name unchanged.
        """
        url = urlparse(self.database_url)
        name = url.path.lstrip("/")
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": name,
            "USER": url.username,
            "PASSWORD": url.password,
            "HOST": url.hostname,
            "PORT": url.port or 5432,
            "CONN_MAX_AGE": 60 if self.is_production else 0,
            "TEST": {"NAME": f"test_{name}{self.test_db_suffix}"},
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
    # Scheduled-task visibility. django_celery_results puts every run in the admin with its
    # status, runtime and return value; django_celery_beat puts the schedule itself in the
    # admin, so "when does this run" and "did it run, and what did it say" are both answerable
    # without reading a log.
    "django_celery_results",
    "django_celery_beat",
    # The Astro dev server and the API are different origins, so the browser preflights every
    # mutating request. Without this the frontend cannot call the API at all in development.
    "corsheaders",
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
    # Serves /static/ from the ASGI process. Django only serves static files itself through
    # `runserver`, and the API runs under uvicorn because SSE needs ASGI — so without this the
    # admin loads with no CSS and no JS, which is exactly how it failed the first time.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Ahead of CommonMiddleware, which is what django-cors-headers requires: a redirect issued by
    # CommonMiddleware (APPEND_SLASH) would otherwise leave the origin without CORS headers, and
    # the browser reports that as an opaque network failure rather than as a redirect.
    "corsheaders.middleware.CorsMiddleware",
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

#: Outside production, WhiteNoise reads through the staticfiles finders, so the admin works from a
#: clean checkout without anyone remembering to run `collectstatic`. In production that is off and
#: the manifest storage below takes over, which fingerprints every file and fails loudly on a
#: missing one instead of serving a 404 into a stylesheet.
WHITENOISE_USE_FINDERS = not settings.is_production
WHITENOISE_AUTOREFRESH = not settings.is_production

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if settings.is_production
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

#: Set on day one. Changing it after the first migration means hand-written surgery
#: across every table holding a user foreign key.
AUTH_USER_MODEL = "accounts.User"

# --- Project -----------------------------------------------------------------------

REDIS_URL = settings.redis_url

# --- CORS ---------------------------------------------------------------------------
# An explicit origin allowlist and nothing wider, and now it is not merely good hygiene: a browser
# refuses a credentialed cross-origin response whose ``Access-Control-Allow-Origin`` is ``*``. The
# moment the access token became a cookie, the wildcard stopped being an option the API could take
# even if someone wanted it. ``CORS_ALLOW_ALL_ORIGINS`` is deliberately not set, in any environment.
CORS_ALLOWED_ORIGINS = settings.cors_origins

#: ``Authorization`` is already one of ``default_headers``, so no custom request header is
#: preflighted any more.
CORS_ALLOW_HEADERS = default_headers

#: Required, and the reason is ``EventSource``. It cannot set request headers, so the only way a
#: browser authenticates ``GET /api/stream`` is the access cookie — which it sends only when the
#: client passes ``withCredentials: true``, and which the browser only accepts back when the server
#: answers with ``Access-Control-Allow-Credentials: true`` and a named origin.
CORS_ALLOW_CREDENTIALS = True

# --- Authentication -------------------------------------------------------------------
# JWT access tokens, minted by ``ninja_jwt`` and validated by ``config.auth``. Not in
# ``INSTALLED_APPS``: the library only needs its settings, and ``ninja_jwt.token_blacklist`` is the
# one part with tables — deliberately not installed, since nothing in this deployment revokes a
# token server-side (``apps.accounts.services.sign_in`` says why).

NINJA_JWT = {
    # Short enough that a leaked access token is worth little, long enough that a working session
    # is not a refresh loop. The refresh token is what carries the session across the day.
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    # Signed with the project's own secret: one secret to rotate, and rotating it revokes every
    # outstanding token at once — which is the only revocation lever this deployment has.
    "SIGNING_KEY": SECRET_KEY,
    "ALGORITHM": "HS256",
}

#: A browser silently discards a ``Secure`` cookie that arrives over plain HTTP, and the compose
#: stack serves ``http://localhost``. So the flag follows the deployment rather than being hardcoded
#: on — production, where it matters, is exactly where it is set.
AUTH_COOKIE_SECURE = settings.is_production

TICKER_INTERVAL_SECONDS = settings.ticker_interval_seconds
STALENESS_THRESHOLD_DAYS = settings.staleness_threshold_days
EVENT_MAX_ATTEMPTS = settings.event_max_attempts

SEED_USER_PASSWORD = settings.seed_user_password
SUPERUSER_USERNAME = settings.superuser_username
SUPERUSER_PASSWORD = settings.superuser_password
SUPERUSER_EMAIL = settings.superuser_email
EVENT_DRAIN_INTERVAL_SECONDS = settings.event_drain_interval_seconds
EVENT_DRAIN_BATCH_SIZE = settings.event_drain_batch_size

#: The one Redis channel the application still speaks on its own behalf: the SSE fan-out reading
#: side of ``GET /api/stream``. Names are configuration, never literals scattered through code.
EVENT_SSE_CHANNEL = "aztec.sse"

# --- Celery ------------------------------------------------------------------------
# Celery is the bus. Producers write the transactional outbox; ``events.drain_outbox`` claims the
# committed rows and dispatches one ``events.handle_event`` per subscribed handler. Redis is the
# broker and nothing else.

CELERY_BROKER_URL = settings.redis_url
# Results land in PostgreSQL, not Redis. Redis results are ephemeral and invisible; a row in
# django_celery_results is durable, queryable and rendered in the admin — which is the whole
# point of being able to audit what the schedule actually did.
CELERY_RESULT_BACKEND = "django-db"

#: Without this, TaskResult stores the return value and almost nothing else — no task name, no
#: arguments, no worker. Those are exactly the columns that make the admin list readable.
CELERY_RESULT_EXTENDED = True

#: Record the STARTED state, so a task that is running right now is distinguishable from one that
#: never began. Without it a hung task looks identical to a task that was never queued.
CELERY_TASK_TRACK_STARTED = True

#: Results are kept for a week and then removed by celery.backend_cleanup, which Beat schedules on
#: its own. Unbounded history is how a task-results table becomes the largest one in the database.
CELERY_RESULT_EXPIRES = 60 * 60 * 24 * 7

#: The schedule lives in the database and is editable from the admin. No seed and no fixture is
#: needed: DatabaseScheduler syncs CELERY_BEAT_SCHEDULE below into PeriodicTask rows every time
#: Beat starts, so a fresh deployment gets its entries without anyone creating them by hand.
#:
#: The trap that follows from that, and it is worth knowing before someone loses an edit: an entry
#: defined below is OWNED by this file. Editing its interval in the admin works until Beat next
#: restarts, and then the sync overwrites it. An entry that an operator should own must be created
#: in the admin only and must never appear here. Beat also installs celery.backend_cleanup by
#: itself, which is what enforces CELERY_RESULT_EXPIRES.
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True

#: Acknowledge after the task returns, not when it is picked up. This is load-bearing now that
#: Celery is the bus: ``drain_outbox`` marks the row published *before* the handler runs, so a
#: worker killed mid-delivery with early acknowledgement would drop an event that the outbox
#: considers dispatched. With late acks the broker redelivers it and ``ProcessedEvent`` absorbs
#: the duplicate. The cost is that a task interrupted by a hard kill runs twice — which every
#: handler already tolerates, by design.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

CELERY_BEAT_SCHEDULE = {
    # The sweeper. ``enqueue_event`` already kicks the drain on commit, so this exists for the one
    # case that kick cannot cover: the broker was down when the transaction committed. Both paths
    # read the same table, so the worst case is a duplicate dispatch, which the ledger absorbs.
    # `description` is not a Celery key — DatabaseScheduler passes unknown entry keys straight
    # through to the PeriodicTask model, so the text below IS the admin description and is
    # re-applied on every Beat start. Writing it here rather than typing it into the admin is what
    # keeps it from being overwritten by the next sync.
    "drain-outbox": {
        "task": "events.drain_outbox",
        "schedule": float(settings.event_drain_interval_seconds),
        "description": (
            "Sweeps the transactional outbox and dispatches each unpublished event to the "
            "handlers registered for its topic. This is the safety net, not the hot path: "
            "writing an event already kicks a drain from transaction.on_commit, so this only "
            "catches what that kick missed — typically a broker that was down at commit time. "
            "Both paths read the same table, so neither can lose an event, and a double dispatch "
            "is absorbed by the ProcessedEvent ledger. A run reporting a rising 'still pending' "
            "count means the worker is falling behind."
        ),
    },
    "emit-interval-tick": {
        "task": "events.emit_interval_tick",
        "schedule": float(settings.ticker_interval_seconds),
        "description": (
            "Emits clock.ticked on a fixed interval. Two priority signals — deadline_pressure "
            "and staleness — are functions of *now* rather than of any change, so a project can "
            "cross its target date or go stale with nobody touching it, and a purely "
            "change-driven system never notices. This tick is what notices. The recalculator "
            "re-scores only the projects whose PriorityScore.valid_until has passed, so a quiet "
            "tick costs one index scan and emits nothing. This interval is therefore the upper "
            "bound on how stale a time-derived score can be."
        ),
    },
    "emit-day-boundary-tick": {
        "task": "events.emit_day_boundary_tick",
        # Local midnight. Overdue and days-open change here, not on the interval grid.
        "schedule": crontab(hour=0, minute=0),
        "description": (
            "Emits clock.ticked once at local midnight. Calendar-derived facts — overdue, days "
            "open — change at the date boundary and not on a five-minute grid, so they get their "
            "own schedule instead of waiting for the next interval tick to notice. Separate from "
            "the interval tick on purpose: the alternative is remembering the last local date in "
            "the process, which is wrong after every restart and duplicated by a second replica."
        ),
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

"""The single place a Redis connection is built for the bus.

Only two modules in the repository are allowed to hold a Redis client: ``handlers.py``, whose
``sse-fanout`` handler publishes, and the SSE endpoint, which subscribes. Redis is no longer the
bus — Celery is — so it carries exactly two things now: the broker, which only Celery touches, and
the pub/sub channel behind ``GET /api/stream``. A module under ``services/`` importing this one is
the dual-write bug described in PATTERNS_BACKEND §1 — that is the rule this module exists to make
visible.
"""

from django.conf import settings
from redis import Redis


def create_redis_client() -> Redis:
    """Open a client against ``settings.REDIS_URL`` with responses decoded to ``str``.

    Decoding is set here rather than at each call site because the channel carries JSON text: a
    client built without it silently hands back ``bytes``, and the resulting type error downstream
    reads like a corrupt event instead of a wiring mistake.

    Returns:
        A connected client. Connection is lazy, so a Redis outage surfaces on the first command as
        a ``redis.exceptions.ConnectionError``, which ``events.handle_event`` treats as a
        retryable failure and finally dead-letters rather than a crash.
    """
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)

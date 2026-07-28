"""The single place a Redis connection is built for the bus.

Only three modules in the repository are allowed to hold a Redis client: ``relay.py``, which
publishes, ``consumers/base.py``, which reads a consumer group, and the SSE endpoint, which
subscribes. A module under ``services/`` importing this one is the dual-write bug described in
PATTERNS_BACKEND §1 — that is the rule this module exists to make visible.
"""

from django.conf import settings
from redis import Redis


def create_redis_client() -> Redis:
    """Open a client against ``settings.REDIS_URL`` with responses decoded to ``str``.

    Decoding is set here rather than at each call site because the whole bus speaks JSON text:
    a client built without it silently hands back ``bytes`` keys, and the resulting
    ``KeyError: 'data'`` on a stream entry reads like a corrupt event instead of a wiring mistake.

    Returns:
        A connected client. Connection is lazy, so a Redis outage surfaces on the first command
        as a ``redis.exceptions.ConnectionError``, which the relay and the runner already treat as
        a retryable failure rather than a crash.
    """
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)

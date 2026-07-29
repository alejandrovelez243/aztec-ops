"""The health API, and the split between its three routes is the whole point of the module.

An orchestrator asks three different questions and they have three different right answers. Giving
them one endpoint is how a dependency outage becomes an application outage.

``/health/live`` — **is the process alive?** No dependency is checked here, ever. If liveness
touched Redis, a Redis outage would fail the probe, Docker would restart the API, the restart
would not fix Redis, and the one process still able to serve cached reads would be in a crash loop
for the duration of the incident. Liveness answers about *this process* and nothing else.

``/health/ready`` — **can it serve a request?** PostgreSQL and Redis are checked, because a request
that reaches this API needs both, and a 503 here takes the instance out of the load balancer's
rotation without killing it. When it fails it says which check failed: "not ready" with no detail
sends an operator to read logs for something the probe already knew.

``/health/pipeline`` — **is the bus keeping up?** Informational, and it **always returns 200**. A
backlog or a poisoned event is a fact about the workers, and wiring it to a container healthcheck
would restart the API because a handler is stuck — the exact inversion of what an operator wants
during an incident. The numbers come from a service in ``apps.events`` so this router never learns
what an ``OutboxEvent`` is.

Every route is unauthenticated: a probe runs before anything can present a header, and none of the
three reveals business data.
"""

import logging
from typing import Final, Literal

from django.db import DatabaseError, connections
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router
from ninja.responses import Status
from pydantic import BaseModel, ConfigDict
from redis.exceptions import RedisError

from apps.events.redis_client import create_redis_client
from apps.events.services import PipelineHealth, read_pipeline_health

logger = logging.getLogger(__name__)

router = Router(tags=["health"])

#: Name of the readiness check covering the primary database.
CHECK_DATABASE: Final = "database"

#: Name of the readiness check covering Redis, which carries the Celery broker and the SSE channel.
CHECK_REDIS: Final = "redis"

#: What a passing check reports. A fixed string rather than an empty one so a rendered dashboard
#: shows the same shape for a pass and a failure.
DETAIL_OK: Final = "ok"


class LivenessOut(BaseModel):
    """Response of ``GET /api/v1/health/live``: the process answered, therefore it is alive.

    Deliberately carries nothing else. Every field added here is a field somebody eventually
    computes, and the first computation that can fail turns liveness into readiness by accident.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["alive"] = "alive"


class DependencyCheck(BaseModel):
    """One readiness probe and what it found.

    ``detail`` carries the exception's own message on failure, truncated by nothing: it is read by
    an operator during an incident, and "connection refused" versus "password authentication
    failed" is the difference between restarting a container and fixing a secret.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    ok: bool
    detail: str


class ReadinessOut(BaseModel):
    """Response of ``GET /api/v1/health/ready``, on both 200 and 503.

    The same shape either way, so a probe or a dashboard parses one thing. ``ready`` is the
    conjunction of the checks and is not independently settable — a payload where ``ready`` is true
    while a check failed is a state this type cannot represent.
    """

    model_config = ConfigDict(frozen=True)

    ready: bool
    checks: tuple[DependencyCheck, ...]

    @classmethod
    def of(cls, checks: tuple[DependencyCheck, ...]) -> "ReadinessOut":
        """Build the response from the checks that ran.

        Args:
            checks: Every probe's result, in a fixed order so two responses are comparable.

        Returns:
            The response, ready exactly when every check passed.
        """
        return cls(ready=all(check.ok for check in checks), checks=checks)


class PipelineOut(BaseModel):
    """Response of ``GET /api/v1/health/pipeline``: the event pipeline's vital signs.

    A thin restatement of :class:`~apps.events.services.PipelineHealth` on purpose. The service's
    type is the context's; this one is the wire's, and keeping them separate is what lets the
    pipeline read grow a field that the public contract does not have to carry.
    """

    model_config = ConfigDict(frozen=True)

    unpublished: int
    oldest_unpublished_age_seconds: float | None
    dead_lettered: int
    last_tick_at: str | None
    last_tick_age_seconds: float | None

    @classmethod
    def of(cls, health: PipelineHealth) -> "PipelineOut":
        """Project the service's snapshot onto the wire shape.

        Args:
            health: What the events context measured.

        Returns:
            The response body, with the tick rendered as an ISO-8601 string like every other
            datetime in the contract.
        """
        return cls(
            unpublished=health.unpublished,
            oldest_unpublished_age_seconds=health.oldest_unpublished_age_seconds,
            dead_lettered=health.dead_lettered,
            last_tick_at=health.last_tick_at.isoformat() if health.last_tick_at else None,
            last_tick_age_seconds=health.last_tick_age_seconds,
        )


@router.get("/health/live", response=LivenessOut, auth=None, url_name="health_live")
def get_liveness(request: HttpRequest) -> LivenessOut:
    """Answer that this process is running. Checks nothing, on purpose.

    A dependency check here would make a Redis or PostgreSQL outage restart the API, which does not
    repair either one and removes the last process able to answer at all. Liveness is about the
    process; readiness is about the request.
    """
    del request
    return LivenessOut()


@router.get(
    "/health/ready",
    response={200: ReadinessOut, 503: ReadinessOut},
    auth=None,
    url_name="health_ready",
)
def get_readiness(request: HttpRequest) -> Status[ReadinessOut]:
    """Report whether this instance can serve a request, with per-check detail when it cannot.

    Every check runs even after one has failed. Short-circuiting would save a round trip and cost
    the operator the second half of the picture — "the database is down" and "the database and
    Redis are both down" call for different responses.

    Returns 503 with the same body shape as the 200, so one parser covers both.
    """
    del request
    checks = (_check_database(), _check_redis())
    body = ReadinessOut.of(checks)
    if not body.ready:
        return Status(503, body)
    return Status(200, body)


@router.get("/health/pipeline", response=PipelineOut, auth=None, url_name="health_pipeline")
def get_pipeline_health(request: HttpRequest) -> PipelineOut:
    """Report the outbox backlog, the dead-letter count and the age of the last clock tick.

    **Always 200, including when every number is alarming.** This route exists to be read by a
    human or scraped by a dashboard, never to gate a container healthcheck: a poisoned event must
    not be able to restart the API.
    """
    del request
    return PipelineOut.of(read_pipeline_health(now=timezone.now()))


def _check_database() -> DependencyCheck:
    """Prove the primary database answers, with the cheapest statement that requires a connection.

    ``SELECT 1`` rather than a model query: a probe that touched a table would start failing on a
    pending migration, which is a deploy problem and not a readiness one.

    Returns:
        The check result. Never raises — a failed probe is data here, not an exception, which is
        the one place in this codebase where catching broadly is the correct behaviour.
    """
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError as error:
        logger.warning("readiness: database check failed", exc_info=True)
        return DependencyCheck(name=CHECK_DATABASE, ok=False, detail=str(error))
    return DependencyCheck(name=CHECK_DATABASE, ok=True, detail=DETAIL_OK)


def _check_redis() -> DependencyCheck:
    """Prove Redis answers a ``PING``, and close the connection whether it did or not.

    Redis carries two things the API needs: the Celery broker a mutating request kicks the drain
    through, and the pub/sub channel ``GET /api/stream`` subscribes to. A leaked client per probe
    would make a frequently-polled readiness endpoint the thing that exhausts the pool.

    Returns:
        The check result. Never raises, for the same reason as the database probe.
    """
    client = create_redis_client()
    try:
        client.ping()
    except (RedisError, OSError) as error:
        logger.warning("readiness: redis check failed", exc_info=True)
        return DependencyCheck(name=CHECK_REDIS, ok=False, detail=str(error))
    else:
        return DependencyCheck(name=CHECK_REDIS, ok=True, detail=DETAIL_OK)
    finally:
        client.close()

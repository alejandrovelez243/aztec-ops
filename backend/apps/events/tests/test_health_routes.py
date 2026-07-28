"""HTTP behaviour of the three health routes, and of the split between them.

The routes are composed in ``config.health`` because they describe the deployment rather than any
bounded context, but their one interesting dependency — the pipeline numbers — belongs to
``apps.events``, and ``testpaths`` only collects ``apps``. They are proved here.

The base classes are the assertions. ``LivenessRouteTestCase`` is a ``SimpleTestCase``, which
*forbids* database access: if liveness ever grows a dependency check, that test fails with a
database error rather than by an assertion somebody has to think about. That is the guarantee the
whole split exists for — a liveness probe that checked Redis would have Docker restart the API
during a Redis outage, which repairs nothing and removes the last process able to serve reads.

Redis is never contacted for real. The client factory is patched where ``config.health`` bound it,
so the readiness tests prove the *mapping* from a probe failure to a 503 with detail, which is the
part that can be wrong.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase
from redis.exceptions import ConnectionError as RedisConnectionError

from apps.events.domain.envelope import ENTITY_PROJECT, SYSTEM_ACTOR, TOPIC_PROJECT_STATE_CHANGED
from apps.events.models import OutboxEvent

LIVE_URL = "/api/v1/health/live"
READY_URL = "/api/v1/health/ready"
PIPELINE_URL = "/api/v1/health/pipeline"


class _FakeRedis:
    """The two methods ``config.health`` calls on a client, and a record of the close."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.closed = False

    def ping(self) -> bool:
        if self.error is not None:
            raise self.error
        return True

    def close(self) -> None:
        self.closed = True


def _patch_redis(client: _FakeRedis) -> Any:
    return patch("config.health.create_redis_client", return_value=client)


class LivenessRouteTestCase(SimpleTestCase):
    def test_liveness_answers_without_touching_the_database(self) -> None:
        # SimpleTestCase raises on any query, so this passing *is* the proof that liveness
        # checks nothing. A dependency check added to that route turns this test red.
        response = self.client.get(LIVE_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "alive"})

    def test_liveness_needs_no_credential(self) -> None:
        self.assertEqual(self.client.get(LIVE_URL).status_code, 200)


class ReadinessRouteTestCase(TestCase):
    def test_every_dependency_answering_is_a_200_naming_each_check(self) -> None:
        with _patch_redis(_FakeRedis()):
            response = self.client.get(READY_URL)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ready"])
        self.assertEqual([check["name"] for check in body["checks"]], ["database", "redis"])
        self.assertTrue(all(check["ok"] for check in body["checks"]))

    def test_an_unreachable_redis_is_a_503_that_says_which_check_failed(self) -> None:
        client = _FakeRedis(error=RedisConnectionError("Connection refused"))
        with _patch_redis(client):
            response = self.client.get(READY_URL)

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertFalse(body["ready"])
        checks = {check["name"]: check for check in body["checks"]}
        self.assertTrue(checks["database"]["ok"])
        self.assertFalse(checks["redis"]["ok"])
        self.assertIn("Connection refused", checks["redis"]["detail"])

    def test_the_redis_probe_closes_its_client_even_when_the_ping_failed(self) -> None:
        client = _FakeRedis(error=RedisConnectionError("down"))
        with _patch_redis(client):
            self.client.get(READY_URL)

        self.assertTrue(client.closed)

    def test_readiness_still_reports_the_database_when_redis_is_down(self) -> None:
        # Both checks run: short-circuiting would tell an operator half of what is broken.
        with _patch_redis(_FakeRedis(error=RedisConnectionError("down"))):
            body = self.client.get(READY_URL).json()

        self.assertEqual(len(body["checks"]), 2)


class PipelineRouteTestCase(TestCase):
    def test_an_idle_pipeline_reports_zeroes_and_no_tick(self) -> None:
        response = self.client.get(PIPELINE_URL)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["unpublished"], 0)
        self.assertEqual(body["dead_lettered"], 0)
        self.assertIsNone(body["last_tick_at"])

    def test_a_backlog_and_a_dead_letter_are_still_a_200(self) -> None:
        # The whole point of this route: an alarming pipeline must never be able to fail a
        # container healthcheck. A poisoned event must not restart the API.
        occurred_at = datetime.now(tz=UTC) - timedelta(hours=2)
        for _ in range(3):
            OutboxEvent.objects.create(
                id=uuid4(),
                topic=TOPIC_PROJECT_STATE_CHANGED,
                entity_type=ENTITY_PROJECT,
                entity_id="PRJ-T1",
                payload={},
                actor=SYSTEM_ACTOR,
                correlation_id=uuid4(),
                occurred_at=occurred_at,
            )
        OutboxEvent.objects.create(
            id=uuid4(),
            topic=TOPIC_PROJECT_STATE_CHANGED,
            entity_type=ENTITY_PROJECT,
            entity_id="PRJ-T1",
            payload={},
            actor=SYSTEM_ACTOR,
            correlation_id=uuid4(),
            occurred_at=occurred_at,
            published_at=occurred_at,
            dead_lettered_at=occurred_at,
            attempts=5,
            last_error="RuntimeError: boom",
        )

        response = self.client.get(PIPELINE_URL)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["unpublished"], 3)
        self.assertEqual(body["dead_lettered"], 1)
        self.assertGreater(body["oldest_unpublished_age_seconds"], 3600)

    def test_the_pipeline_route_needs_no_credential(self) -> None:
        self.assertEqual(self.client.get(PIPELINE_URL).status_code, 200)

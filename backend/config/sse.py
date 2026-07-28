"""``GET /api/stream`` — the browser's only window onto the event bus.

Unversioned on purpose (`docs/API.md` §1.1): a tab keeps this connection open across deploys, and
the envelope carries its own ``version`` field, so versioning the path would break every open
dashboard on every release.

**Async, and it has to be.** A WSGI worker holds a thread per open stream, so a handful of
dashboards left open overnight exhausts the pool. This view is a coroutine served by ASGI, and
every await inside it is non-blocking: the subscription uses ``redis.asyncio``, and the idle wait
is ``get_message(timeout=…)``, which yields to the loop instead of parking a thread.

**The subscription is always torn down.** A leaked ``pubsub`` per dropped browser is how this dies
in production — the connection pool fills with subscribers nobody is reading. When the client
disconnects, the generator is closed, and the ``finally`` below unsubscribes and closes the client
on every path including cancellation.

**The heartbeat is not decoration.** An idle SSE connection with no bytes on it is indistinguishable
from a dead one to a proxy or a load balancer, and it gets reaped. A comment frame every
:data:`HEARTBEAT_SECONDS` keeps it alive; comments carry no ``id``, so they never disturb
``Last-Event-ID``.

**``X-Accel-Buffering: no`` is load-bearing.** Without it a buffering proxy holds frames until a
buffer fills, and the page looks frozen while the data is already on the wire.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Final

import redis.asyncio as aioredis
from django.conf import settings
from django.http import HttpRequest, StreamingHttpResponse

from apps.events.domain.envelope import SSE_ALLOWLIST_TOPICS

logger = logging.getLogger(__name__)

#: Seconds between comment frames while the stream is idle. `docs/API.md` §3.4 fixes 15 and pairs
#: it with a 45-second client-side death detector, so three consecutive misses are needed before a
#: client gives up — one dropped heartbeat must not cause a reconnect storm.
HEARTBEAT_SECONDS: Final = 15.0

#: The browser's own reconnect floor, sent once immediately after the connection opens. Without it
#: a browser uses its default (often 3s in one engine and much less in another), and a backend
#: restart turns every open tab into a retry loop at a rate the server did not choose.
RETRY_MILLISECONDS: Final = 3000

#: Sent when the client asked to resume from an event this transport cannot replay. The client
#: refetches the affected resources instead of trusting its local state (`docs/API.md` §3.5).
TOPIC_STREAM_RESET: Final = "stream.reset"

#: Redis pub/sub is a *live* channel with no history: a subscriber receives what is published after
#: it subscribes and nothing before. So a ``Last-Event-ID`` can never be honoured by replay here —
#: it is answered honestly with one reset frame, which is the escape hatch §3.5 already defines for
#: an id older than the retention window. Replaying from the id itself would need a Redis Streams
#: read against ``aztec.events``, and that is a change to the fan-out contract, not to this view.
RESET_REASON_NO_REPLAY: Final = "last_event_id_expired"


async def event_stream(request: HttpRequest) -> StreamingHttpResponse:
    """Open one server-sent event stream for this client.

    Query parameters (all optional):

    * ``topics`` — comma-separated allowlist subset. Default: every forwarded topic.
    * ``project`` — restrict to one project code, its tasks and its blockers.
    * ``last_event_id`` — same meaning as the ``Last-Event-ID`` header, for a manual reconnect that
      builds a fresh ``EventSource`` and therefore cannot send the header.

    ``X-Actor`` is not required and is ignored: the stream is read-only and not actor-scoped.

    Args:
        request: The incoming ASGI request.

    Returns:
        A streaming response whose body is produced lazily, frame by frame, until the client
        disconnects.
    """
    topics = _requested_topics(request.GET.get("topics"))
    project_code = request.GET.get("project") or None
    last_event_id = request.headers.get("Last-Event-ID") or request.GET.get("last_event_id")

    response = StreamingHttpResponse(
        _frames(topics=topics, project_code=project_code, last_event_id=last_event_id),
        content_type="text/event-stream; charset=utf-8",
    )
    # ``no-transform`` matters as much as ``no-cache``: a proxy that gzips the body buffers it to
    # do so, which is the same frozen page ``X-Accel-Buffering`` prevents.
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    response["Connection"] = "keep-alive"
    return response


def _requested_topics(raw: str | None) -> frozenset[str]:
    """Intersect the caller's ``topics`` with the fan-out allowlist.

    An intersection rather than a validation: a client asking for a topic that is not forwarded
    gets nothing for it, which is the same outcome as the topic not existing, and rejecting the
    request would break a client that had simply been deployed ahead of a topic's removal. An empty
    or absent parameter means "everything the fan-out forwards".
    """
    if not raw:
        return SSE_ALLOWLIST_TOPICS
    requested = {name.strip() for name in raw.split(",") if name.strip()}
    return frozenset(requested & SSE_ALLOWLIST_TOPICS) or SSE_ALLOWLIST_TOPICS


async def _frames(
    *,
    topics: frozenset[str],
    project_code: str | None,
    last_event_id: str | None,
) -> AsyncIterator[str]:
    """Yield SSE frames until the client goes away, then tear the subscription down.

    The ``finally`` is the whole reliability story of this module. Django closes an async generator
    when the client disconnects, which raises ``GeneratorExit`` inside the ``await``; without the
    cleanup below, every dropped browser would leave a subscribed connection in the pool and the
    process would slowly stop being able to open new ones.
    """
    client = aioredis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(settings.EVENT_SSE_CHANNEL)
        yield f"retry: {RETRY_MILLISECONDS}\n\n"
        if last_event_id:
            yield _reset_frame()
        # One heartbeat up front, so a proxy sees bytes immediately rather than after the first
        # idle interval — some of them close a response that produces nothing at all.
        yield ": ping\n\n"

        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=HEARTBEAT_SECONDS
            )
            if message is None:
                yield ": ping\n\n"
                continue
            frame = _frame_for(message.get("data"), topics=topics, project_code=project_code)
            if frame is not None:
                yield frame
    finally:
        # Never re-raise from here: the client is already gone on the disconnect path, and an error
        # while closing would mask the reason the stream ended.
        try:
            # redis-py ships ``aclose`` without annotations on the pubsub object; the call is the
            # documented teardown, so the untyped-call check is silenced here rather than the
            # cleanup being skipped.
            await pubsub.aclose()  # type: ignore[no-untyped-call]
            await client.aclose()
        except Exception:
            logger.warning("failed to close the SSE subscription cleanly", exc_info=True)


def _reset_frame() -> str:
    """The frame that tells a reconnecting client its local state cannot be trusted."""
    body = json.dumps({"reason": RESET_REASON_NO_REPLAY}, separators=(",", ":"))
    return f"event: {TOPIC_STREAM_RESET}\ndata: {body}\n\n"


def _frame_for(
    raw: object,
    *,
    topics: frozenset[str],
    project_code: str | None,
) -> str | None:
    """Render one published envelope as an SSE frame, or ``None`` when it is filtered out.

    The envelope is forwarded byte-for-byte in ``data:``, exactly as the fan-out published it, so
    what a developer sees in devtools is comparable with the outbox row. It is *parsed* here only
    to read ``id``, ``topic`` and ``entity`` for the frame header and the filters — reshaping it
    would give the browser a third version of a fact the API and the read model already state.

    A message that is not a well-formed envelope is dropped with a log line rather than raised on:
    one malformed publish must not close every open dashboard.
    """
    if not isinstance(raw, str):
        return None
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("dropped a malformed SSE message", extra={"channel_payload": raw[:200]})
        return None
    if not isinstance(envelope, dict):
        return None

    topic = envelope.get("topic")
    if not isinstance(topic, str) or topic not in topics:
        return None
    if not _matches_project(envelope, project_code):
        return None

    event_id = envelope.get("id")
    # ``data:`` must never contain a raw newline: the envelope is serialised with no indentation,
    # and a multi-line body would need one ``data:`` line per fragment for the client to rejoin.
    body = raw.replace("\n", "")
    return f"id: {event_id}\nevent: {topic}\ndata: {body}\n\n"


def _matches_project(envelope: dict[str, object], project_code: str | None) -> bool:
    """Whether this event concerns the requested project, its tasks or its blockers.

    Task codes are prefixed with their project (``PRJ-01-T02``), so they match structurally.
    Blockers and notes are not — they carry global codes (``BLK-0142``) — so their events are
    matched on the ``project_code`` their payload carries, which is exactly why every ``blocker.*``
    and ``note.*`` payload carries one.
    """
    if project_code is None:
        return True

    entity = envelope.get("entity")
    entity_id = entity.get("id") if isinstance(entity, dict) else None
    if isinstance(entity_id, str) and (
        entity_id == project_code or entity_id.startswith(f"{project_code}-")
    ):
        return True

    payload = envelope.get("payload")
    return isinstance(payload, dict) and payload.get("project_code") == project_code

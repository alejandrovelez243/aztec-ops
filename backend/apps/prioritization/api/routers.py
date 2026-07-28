"""Routes of the prioritization context: forcing a rank, taking the force back, and rebuilding one.

The rebuild routes are the replacement for ``manage.py recompute``. A command run from a laptop
against the production database has no audit trail, no permission boundary and no record that it
happened; the same use case reached over HTTP runs inside the deployment, carries the ``X-Actor``
header like every other mutation, and is the same function the admin action calls.
"""

from uuid import uuid4

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router
from ninja.responses import Status

from apps.prioritization.api.schemas import OverrideIn, OverrideOut, RecomputeOut
from apps.prioritization.services import recompute_active_portfolio, recompute_projects
from apps.prioritization.services.apply_override import ApplyOverrideCommand
from apps.prioritization.services.override_priority import override_priority
from apps.prioritization.services.revoke_priority_override import revoke_priority_override
from config.actor import actor_code_of, actor_header

router = Router(tags=["prioritization"])


@router.post(
    "/projects/{project_code}/priority-override",
    response=OverrideOut,
    auth=actor_header,
    url_name="priority_override_apply",
)
def post_priority_override(
    request: HttpRequest, project_code: str, payload: OverrideIn
) -> OverrideOut:
    """Force a project's position in the queue, on the record.

    The computed ``score.value`` is returned unchanged: the override is stored separately, so the
    ranking stays auditable and reversible and the UI can show both numbers. Emits a
    ``PRIORITY_CHANGED`` record with ``metadata.origin = "MANUAL"``, correlated with the records of
    anything the move displaced.
    """
    priority = override_priority(
        command=ApplyOverrideCommand(
            project_code=project_code,
            actor=actor_code_of(request),
            reason=payload.reason,
            position=payload.position,
            boost=payload.boost,
            expires_at=payload.expires_at,
        ),
        correlation_id=uuid4(),
        now=timezone.now(),
    )
    return OverrideOut(
        code=project_code,
        score=priority.score,
        override=priority.override,
    )


@router.delete(
    "/projects/{project_code}/priority-override",
    response={204: None},
    auth=actor_header,
    url_name="priority_override_revoke",
)
def delete_priority_override(request: HttpRequest, project_code: str) -> Status[None]:
    """Lift the override, restoring the engine's ranking with no recomputation.

    Idempotent: revoking when nothing is in force writes no record and emits no event, so a client
    retrying after a dropped response gets the same 204 and the timeline does not grow a second
    entry for one revert. The override row is revoked, never deleted — it is the evidence that
    somebody forced a rank and why.
    """
    revoke_priority_override(
        project_code=project_code,
        actor=actor_code_of(request),
        correlation_id=uuid4(),
        now=timezone.now(),
    )
    return Status(204, None)


@router.post(
    "/projects/{project_code}/recompute",
    response=RecomputeOut,
    auth=actor_header,
    url_name="project_recompute",
)
def post_project_recompute(request: HttpRequest, project_code: str) -> RecomputeOut:
    """Rebuild one project's score and risk flags now, instead of waiting for an event.

    The automatic path is unaffected and remains the normal one: a data change publishes an event
    and the ``priority-recalculator`` reacts, while a clock tick covers the time-derived signals.
    This route exists for the two moments nothing will arrive — a policy version was just activated,
    or an operator distrusts a row — and it is deliberately a ``POST``: it writes.

    It emits no event. Broadcasting ``project.priority.recalculated`` for a rebuild that reproduces
    the number already stored would tell every open dashboard that something happened when nothing
    did; the response carries ``changed`` so the caller knows either way.
    """
    del request
    run = recompute_projects(project_codes=(project_code,), now=timezone.now())
    return RecomputeOut.of(run)


@router.post("/recompute", response=RecomputeOut, auth=actor_header, url_name="portfolio_recompute")
def post_portfolio_recompute(request: HttpRequest) -> RecomputeOut:
    """Rebuild the whole active portfolio against the active policy, at one instant.

    A separate route rather than the same one with an optional project, because "rescore everything"
    and "rescore this" have different blast radii and a client should have to say which it meant
    (CLAUDE.md rule 13). Every project is scored for the same ``ran_at``, so two projects with the
    same facts cannot end up ranked apart by how long the loop took.

    Archived projects are skipped: they are out of the queue by definition.
    """
    del request
    return RecomputeOut.of(recompute_active_portfolio(now=timezone.now()))

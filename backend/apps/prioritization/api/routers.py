"""Routes of the prioritization context: forcing a rank, taking the force back, and rebuilding one.

The rebuild routes are the replacement for ``manage.py recompute``. A command run from a laptop
against the production database has no audit trail, no permission boundary and no record that it
happened; the same use case reached over HTTP runs inside the deployment, is attributed to the
signed-in account like every other mutation, and is the same function the admin action calls.

**This module holds both of the product's privileged routes, and only those two.** Everything else
in Aztec Ops is collaborative — any member may transition any project, raise a blocker on anyone's
work, add a note. Two things are not, because both step outside the engine rather than feed it:
forcing a rank against the computed score, and rebuilding the ranking of the whole portfolio. They
declare ``auth=ops_lead``; ``POST /projects/{code}/recompute`` deliberately does not, because
rebuilding one row is idempotent derivation with a blast radius of one project.

Revoking an override is likewise ops-lead: whoever may force a rank must be the one who lifts it,
or the audit trail records a decision that somebody else silently undid.
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
from config.auth import actor_code_of, ops_lead

router = Router(tags=["prioritization"])


@router.post(
    "/projects/{project_code}/priority-override",
    response=OverrideOut,
    auth=ops_lead,
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

    **Ops lead only** (``403 permission_denied`` otherwise). This is the one place a person
    overrules the ranking engine for the whole board, and a queue anybody can reorder is not a
    prioritized queue.
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
    auth=ops_lead,
    url_name="priority_override_revoke",
)
def delete_priority_override(request: HttpRequest, project_code: str) -> Status[None]:
    """Lift the override, restoring the engine's ranking with no recomputation.

    Idempotent: revoking when nothing is in force writes no record and emits no event, so a client
    retrying after a dropped response gets the same 204 and the timeline does not grow a second
    entry for one revert. The override row is revoked, never deleted — it is the evidence that
    somebody forced a rank and why.

    **Ops lead only** (``403 permission_denied`` otherwise), for symmetry with applying one: a rank
    that one person may force and anybody may lift is not a decision, it is a suggestion.
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


@router.post("/recompute", response=RecomputeOut, auth=ops_lead, url_name="portfolio_recompute")
def post_portfolio_recompute(request: HttpRequest) -> RecomputeOut:
    """Rebuild the whole active portfolio against the active policy, at one instant.

    A separate route rather than the same one with an optional project, because "rescore everything"
    and "rescore this" have different blast radii and a client should have to say which it meant
    (CLAUDE.md rule 13). Every project is scored for the same ``ran_at``, so two projects with the
    same facts cannot end up ranked apart by how long the loop took.

    Archived projects are skipped: they are out of the queue by definition.

    **Ops lead only** (``403 permission_denied`` otherwise). Not because rescoring is dangerous —
    it writes derived numbers and emits nothing — but because it is portfolio-wide and expensive,
    and a route any member can hammer against every project is a denial-of-service surface with a
    friendly name. The single-project route beside it stays open to every member.
    """
    del request
    return RecomputeOut.of(recompute_active_portfolio(now=timezone.now()))

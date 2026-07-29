"""Use case: edit the mutable fields of an existing project."""

from collections.abc import Callable
from datetime import datetime
from typing import Final
from uuid import UUID

from django.db import transaction
from pydantic import JsonValue

from apps.activity.domain.value_objects import ActivityCommand
from apps.activity.services import write_activity
from apps.events.domain.changes import render_change_value
from apps.events.domain.envelope import ENTITY_PROJECT, TOPIC_PROJECT_UPDATED
from apps.events.services import enqueue_event
from apps.portfolio.domain.errors import ProjectNotFound
from apps.portfolio.domain.rules import (
    ensure_non_negative_business_value,
    ensure_valid_date_window,
)
from apps.portfolio.domain.value_objects import ProjectResult, UpdateProjectCommand
from apps.portfolio.models import Project
from apps.portfolio.services._references import (
    resolve_client,
    resolve_currency,
    resolve_engagement_type,
    resolve_owner,
    resolve_project_type,
    resolve_stage,
)

#: Command fields copied straight onto the aggregate.
_SCALAR_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "start_date",
    "target_date",
    "business_value",
    "summary",
    "description",
    "next_step",
    "is_archived",
)

#: Command fields that name another row, mapped to the model attribute and its resolver.
_REFERENCE_FIELDS: Final[dict[str, tuple[str, Callable[[str | None], object]]]] = {
    "client_code": ("client", resolve_client),
    "currency_code": ("currency", resolve_currency),
    "engagement_type_code": ("engagement_type", resolve_engagement_type),
    "project_type_code": ("project_type", resolve_project_type),
    "stage_code": ("stage", resolve_stage),
    "owner_code": ("owner", resolve_owner),
}

#: The only fields where an explicitly sent ``null`` means "clear this". Everywhere else the column
#: is NOT NULL, so a null is not an instruction the domain can honour and is ignored rather than
#: turned into an empty string that would look like a deliberate blanking.
_CLEARABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"start_date", "target_date", "business_value", "project_type_code", "stage_code", "owner_code"}
)


@transaction.atomic
def update_project(
    command: UpdateProjectCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> ProjectResult:
    """Apply a partial edit to a project, auditing the two changes the operation reads.

    ``workflow_state`` is not editable here under any name — a state change goes through
    ``transition_project`` so it is validated against ``WorkflowTransition`` (CLAUDE.md rule 2).

    Only the fields the caller actually sent are touched, and only those whose value really differs
    are written; an edit that changes nothing returns the current projection without an
    ``ActivityRecord`` and without an ``OutboxEvent``, because a no-op that emits an event teaches
    consumers to recompute for nothing and fills the timeline with noise.

    An owner change and a next-step change each append their own ``ActivityRecord`` (``OWNER_CHANGED``,
    ``NEXT_STEP_SET``): both are decisions the operation reviews later. Other field edits are
    carried by the ``project.updated`` event alone, which names each changed field with its before
    and after value.

    Args:
        command: The project code plus the subset of fields to change.
        actor: ``accounts.User.code``, or "system".
        correlation_id: Threaded from the API boundary.
        now: Domain time, supplied by the caller so replays are deterministic.

    Returns:
        The project as it stands after the edit.

    Raises:
        ProjectNotFound: No project carries ``command.code``.
        ClientNotFound: A supplied client code matches no client.
        TaxonomyEntryNotFound: A supplied taxonomy code matches no row.
        OwnerNotFound: A supplied owner code matches nobody.
        InvalidDateWindow: The resulting start/target pair is inverted.
        NegativeBusinessValue: The resulting business value is below zero.
    """
    project = Project.objects.locked().with_relations().by_code(command.code).first()
    if project is None:
        raise ProjectNotFound(command.code)

    previous_owner_code = project.owner.code if project.owner else ""
    previous_next_step = project.next_step

    changes = _apply_updates(project, command)
    changed_fields = sorted(changes)
    if not changed_fields:
        return project.to_result()

    ensure_valid_date_window(project.start_date, project.target_date)
    ensure_non_negative_business_value(project.business_value)
    project.save(update_fields=[*changed_fields, "updated_at"])

    if "owner" in changed_fields:
        write_activity(
            ActivityCommand(
                entity_type=ENTITY_PROJECT,
                entity_id=project.code,
                verb="OWNER_CHANGED",
                actor=actor,
                from_value=previous_owner_code,
                to_value=project.owner.code if project.owner else "",
                occurred_at=now,
                correlation_id=correlation_id,
            )
        )
    if "next_step" in changed_fields:
        write_activity(
            ActivityCommand(
                entity_type=ENTITY_PROJECT,
                entity_id=project.code,
                verb="NEXT_STEP_SET",
                actor=actor,
                from_value=previous_next_step,
                to_value=project.next_step,
                occurred_at=now,
                correlation_id=correlation_id,
            )
        )

    # EVENTS.md §4 `project.updated`: one non-empty object mapping field name to its before and
    # after value. Consumers diff on it, so the names are model field names, not command names.
    payload: dict[str, JsonValue] = {
        "changes": {field: dict(pair) for field, pair in changes.items()}
    }
    enqueue_event(
        topic=TOPIC_PROJECT_UPDATED,
        entity_type=ENTITY_PROJECT,
        entity_id=project.code,
        payload=payload,
        actor=actor,
        correlation_id=correlation_id,
        occurred_at=now,
    )

    return project.to_result()


def _apply_updates(
    project: Project, command: UpdateProjectCommand
) -> dict[str, dict[str, JsonValue]]:
    """Mutate the in-memory aggregate and report what actually moved, as before/after pairs.

    Keys are model field names, not command field names, so the caller can hand them to
    ``update_fields`` and to the ``project.updated`` payload without a second mapping. Values are
    already JSON-shaped: the payload is validated inside the producing transaction, so a ``date``
    or a ``Decimal`` reaching ``enqueue_event`` would abort the edit rather than the publish.
    """
    changed: dict[str, dict[str, JsonValue]] = {}

    for field_name in _SCALAR_FIELDS:
        if not command.was_provided(field_name):
            continue
        new_value = getattr(command, field_name)
        if new_value is None and field_name not in _CLEARABLE_FIELDS:
            continue
        old_value = getattr(project, field_name)
        if old_value == new_value:
            continue
        setattr(project, field_name, new_value)
        changed[field_name] = {
            "from": render_change_value(old_value),
            "to": render_change_value(new_value),
        }

    for command_field, (model_field, resolve) in _REFERENCE_FIELDS.items():
        if not command.was_provided(command_field):
            continue
        supplied_code = getattr(command, command_field)
        if supplied_code is None and command_field not in _CLEARABLE_FIELDS:
            continue
        new_reference = resolve(supplied_code)
        old_reference = getattr(project, model_field)
        if old_reference == new_reference:
            continue
        setattr(project, model_field, new_reference)
        changed[model_field] = {
            "from": render_change_value(old_reference),
            "to": render_change_value(new_reference),
        }

    return changed

"""Use case: reword, recolour, recategorise or move a column.

**Absent means untouched, and only what moved is written.** Sending a node its current label and its
current colour is a successful no-op that writes no trail entry, because a trail that records
non-changes cannot be read for changes.

One entry per *edit*, not one per field. A node's label, category, colour and order are one act of
authoring performed in one form, so four verbs would report four decisions where an operator made
one; which fields moved, and from what, is ``metadata.changed``. That is the opposite choice from
``ActivityRecord`` verbs like ``OWNER_CHANGED`` and ``CAPACITY_CHANGED``, and the difference is real:
those are separate operational facts with separate readers, while nobody asks "when was this column
recoloured" independently of "when was this column edited".

``code`` is not editable. Every project and task on this node holds its primary key and every
document already published names it by slug; renaming is what ``label`` is for. ``is_initial`` is not
editable either — moving the entry node is a decision about where new work starts, defended by a
partial unique constraint, and it stays in the admin.

``is_active`` only ever moves one way here: ``true``, restoring a node somebody retired. Retiring is
``DELETE`` (:mod:`~apps.workflow.services.retire_state`) because it can be refused by records this
request knows nothing about and because it withdraws every arrow touching the node — a field that
sometimes fails for reasons unrelated to itself is a field a form cannot explain.
"""

from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.activity.models import ActivityRecord
from apps.activity.services import write_activity
from apps.workflow.domain.commands import UpdateStateCommand
from apps.workflow.domain.views import WorkflowShapeView
from apps.workflow.services._authoring import (
    FieldChange,
    authoring_record,
    changed_fields,
    checked_category,
    locked_workflow,
    renaming,
    shape_of,
    state_of,
)

#: The columns this use case may write. Listed once so ``update_fields`` cannot drift from the
#: branches below it — a field assigned and left out of the list is a change that silently vanishes.
_EDITABLE_FIELDS = ["label", "category", "color", "order", "is_active"]


@transaction.atomic
def update_state(
    command: UpdateStateCommand,
    *,
    actor: str,
    correlation_id: UUID,
    now: datetime,
) -> WorkflowShapeView:
    """Apply the node fields that were sent and differ, and record the edit once.

    Args:
        command: Which graph, which node, and which fields to touch.
        actor: ``accounts.User.code`` of the ops lead editing it.
        correlation_id: Threaded from the API boundary.
        now: Domain time, supplied by the caller.

    Returns:
        The whole graph as it now stands.

    Raises:
        WorkflowNotFound: ``command.workflow_code`` matches no graph.
        WorkflowStateNotFound: The graph has no node with that code — which is also the answer when
            the code names a node of another graph.
        ValueOutsideVocabulary: ``category`` is outside the five.
    """
    workflow = locked_workflow(command.workflow_code)
    state = state_of(workflow, command.code)
    sent = command.model_fields_set
    changed: list[FieldChange] = []

    if "label" in sent and command.label is not None and command.label != state.label:
        changed.append(FieldChange(field="label", before=state.label, after=command.label))
        state.label = command.label

    if "category" in sent and command.category is not None:
        category = checked_category(command.category)
        if category != state.category:
            changed.append(FieldChange(field="category", before=state.category, after=category))
            state.category = category

    if "color" in sent and command.color is not None and command.color != state.color:
        changed.append(FieldChange(field="color", before=state.color, after=command.color))
        state.color = command.color

    if "order" in sent and command.order is not None and command.order != state.order:
        changed.append(
            FieldChange(field="order", before=str(state.order), after=str(command.order))
        )
        state.order = command.order

    # Restore only. ``Literal[True]`` on the command is what makes "retire through PATCH"
    # unrepresentable, because retiring can be refused by records this request never looked at.
    if "is_active" in sent and command.is_active is True and not state.is_active:
        changed.append(FieldChange(field="is_active", before="false", after="true"))
        state.is_active = True

    if not changed:
        return shape_of(workflow)

    state.save(update_fields=_EDITABLE_FIELDS)
    rename = renaming(changed)
    write_activity(
        authoring_record(
            workflow_code=workflow.code,
            verb=ActivityRecord.Verb.STATE_EDITED,
            actor=actor,
            now=now,
            correlation_id=correlation_id,
            before=rename.before if rename is not None else "",
            after=rename.after if rename is not None else "",
            metadata={"state": state.code, "changed": changed_fields(changed)},
        )
    )
    return shape_of(workflow)

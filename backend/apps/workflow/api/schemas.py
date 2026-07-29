"""Request bodies of the authoring routes.

Only inputs live here. Every write answers with
:class:`~apps.workflow.domain.views.WorkflowShapeView` — the same document ``GET /api/v1/workflows``
publishes — so there is no ``*Out`` class restating fields the read side already fixes, and an
editor re-renders from the shape it already knows how to read.

The bodies deliberately do **not** mirror the commands one for one: what identifies a graph, a node
or an edge lives in the path, because those are the addresses of the resources being changed. A
``code`` in the body of a ``PATCH`` would be a second, disagreeing address.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from apps.workflow.domain.commands import (
    COLOR_PATTERN,
    GUARD_MAX_LENGTH,
    LABEL_MAX_LENGTH,
    ORDER_MAX,
    STATE_CODE_MAX_LENGTH,
    WORKFLOW_CODE_MAX_LENGTH,
    WORKFLOW_NAME_MAX_LENGTH,
)


class WorkflowCreateIn(BaseModel):
    """Body of ``POST /api/v1/workflows``.

    The graph arrives empty; states and transitions are added afterwards, each independently
    retryable and independently recorded.

    ``is_default`` is absent on purpose: which graph unbound work falls back to is a decision about
    the whole portfolio, not a checkbox on the form that adds a lifecycle.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=WORKFLOW_CODE_MAX_LENGTH)
    name: str = Field(min_length=1, max_length=WORKFLOW_NAME_MAX_LENGTH)
    applies_to: str = Field(min_length=1, max_length=8)
    engagement_types: list[str] = Field(default_factory=list)


class WorkflowUpdateIn(BaseModel):
    """Body of ``PATCH /api/v1/workflows/{workflow_code}``. Absent means untouched.

    ``code`` and ``applies_to`` are not here. The first is the address; the second would leave the
    records already inside the graph governed by a lifecycle that claims to govern something else.
    """

    model_config = ConfigDict(frozen=True)

    name: str | None = Field(default=None, min_length=1, max_length=WORKFLOW_NAME_MAX_LENGTH)
    is_active: bool | None = None


class StateCreateIn(BaseModel):
    """Body of ``POST /api/v1/workflows/{workflow_code}/states``.

    ``order`` is optional and absent **appends**: a new column belongs at the end of the arrangement
    somebody already made, not at position zero in front of it.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=STATE_CODE_MAX_LENGTH)
    label: str = Field(min_length=1, max_length=LABEL_MAX_LENGTH)
    category: str = Field(min_length=1, max_length=16)
    color: str = Field(default="", pattern=COLOR_PATTERN)
    order: int | None = Field(default=None, ge=0, le=ORDER_MAX)


class StateUpdateIn(BaseModel):
    """Body of ``PATCH /api/v1/workflows/{workflow_code}/states/{state_code}``. Absent = untouched.

    ``is_active`` accepts only ``true``. Restoring a retired node is always safe; retiring one can be
    refused by the records standing on it, so it is ``DELETE`` and not a field that sometimes fails
    for a reason unrelated to itself.
    """

    model_config = ConfigDict(frozen=True)

    label: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX_LENGTH)
    category: str | None = Field(default=None, min_length=1, max_length=16)
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    order: int | None = Field(default=None, ge=0, le=ORDER_MAX)
    is_active: Literal[True] | None = None


class TransitionCreateIn(BaseModel):
    """Body of ``POST /api/v1/workflows/{workflow_code}/transitions``.

    Both endpoints are state **codes inside this graph** — resolved against the workflow in the
    path, which is what makes an edge across two lifecycles impossible to express rather than merely
    rejected. ``guard`` is one of the codes ``GET /api/v1/workflows/guards`` lists.
    """

    model_config = ConfigDict(frozen=True)

    from_state: str = Field(min_length=1, max_length=STATE_CODE_MAX_LENGTH)
    to_state: str = Field(min_length=1, max_length=STATE_CODE_MAX_LENGTH)
    label: str = Field(min_length=1, max_length=LABEL_MAX_LENGTH)
    requires_reason: bool = False
    requires_fields: list[str] = Field(default_factory=list)
    guard: str = Field(default="", max_length=GUARD_MAX_LENGTH)
    order: int | None = Field(default=None, ge=0, le=ORDER_MAX)


class TransitionUpdateIn(BaseModel):
    """Body of ``PATCH .../transitions/{from_state_code}/{to_state_code}``. Absent = untouched.

    The endpoints are the address and are not editable: an edge *is* its endpoints, so repointing an
    arrow is withdrawing one move and declaring another. ``requires_fields`` is sent whole, because
    patching one entry of a list has no unambiguous spelling. ``is_active`` accepts only ``true``,
    which is how a withdrawn move comes back — withdrawing it is ``DELETE``.
    """

    model_config = ConfigDict(frozen=True)

    label: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX_LENGTH)
    requires_reason: bool | None = None
    requires_fields: list[str] | None = None
    guard: str | None = Field(default=None, max_length=GUARD_MAX_LENGTH)
    order: int | None = Field(default=None, ge=0, le=ORDER_MAX)
    is_active: Literal[True] | None = None

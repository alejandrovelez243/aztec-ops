"""What the portfolio context publishes to a read surface: the queue row and the project detail.

These are the two shapes the command center is built from, and they come from **different sides of
the system on purpose**.

:class:`QueueItemView` is served entirely from ``ProjectSnapshot`` — one row, one index scan, no
join (ARCHITECTURE §8). It therefore carries only what the read model denormalizes, which is why
its taxonomy fields are codes where the snapshot stores no label, and why its ``override`` is the
two columns the snapshot keeps rather than the full record.

:class:`ProjectDetailView` is served from the write side, so it is never stale by a rebuild and it
carries the full references, the tasks, the blockers, the score's whole argument and — the
load-bearing part — the legal transitions.

This module composes value objects owned by ``prioritization``, ``work`` and ``workflow``. That is
an import between pure, Django-free ``domain/`` modules and nothing more: a published-language
dependency, not a dependency on another context's models, services or database (ARCHITECTURE §7).
The composition is what a *read* of the portfolio is — a queue row that could not name a score or a
risk flag would be a queue nobody could act on.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from apps.prioritization.domain.views import OverrideView, RiskFlagView, ScoreView
from apps.shared.refs import ActorRef, StateRef, TaxonomyRef
from apps.work.domain.views import BlockerView, NoteView, TaskView
from apps.workflow.domain.views import TransitionOption, WorkflowRef

#: Display text for the three derived health values, in the source spreadsheet's own Spanish
#: vocabulary (ARCHITECTURE §5) because the interface is Spanish (PRODUCT.md) — the same exception
#: the database's user-facing labels have; identifiers and documentation stay English
#: (CLAUDE.md §Language). Do not translate these back.
#:
#: This is a closed structural set of exactly three derived values, not an extension point: unlike
#: a risk flag or a signal, no deploy can add a fourth, so naming them here costs nothing and
#: keeps every surface spelling them the same way.
HEALTH_LABELS: dict[str, str] = {
    "HEALTHY": "Sano",
    "AT_RISK": "En riesgo",
    "BLOCKED": "Bloqueado",
}


class HealthRef(BaseModel):
    """Derived project health, ready to render.

    Structural rather than operator data: a pure function of the open risk flags, so the client
    branches on ``code`` and renders ``label``. It is never writable — `docs/API.md` §2.4 rejects
    an attempt to set it — because a health somebody typed would immediately disagree with the
    flags it is supposed to summarise.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    label: str

    @classmethod
    def of(cls, code: str) -> "HealthRef":
        """Build the reference for a derived health code.

        An unrecognised code renders as itself rather than raising: health is derived, so an
        unknown value means the derivation changed ahead of this table, and showing the raw code
        is more useful to whoever has to notice that than a 500.

        Args:
            code: ``HEALTHY`` | ``AT_RISK`` | ``BLOCKED``.

        Returns:
            The reference with its display label.
        """
        return cls(code=code, label=HEALTH_LABELS.get(code, code))


class QueueOverrideView(BaseModel):
    """The fact that a queue row was manually forced, as the read model records it.

    Narrower than :class:`~apps.prioritization.domain.views.OverrideView` on purpose. The snapshot
    denormalizes the position and the reason — what the row has to *show* — and not the actor or
    the timestamps, which belong to the audit and are served with the project detail. Widening it
    would mean widening the read model to satisfy a list view that already fits in one index scan.
    """

    model_config = ConfigDict(frozen=True)

    position: int | None = None
    reason: str


class QueueItemView(BaseModel):
    """One row of the prioritized queue, read from ``ProjectSnapshot`` alone.

    ``score.value`` is the computed number and is never rewritten by ``override``: when
    ``override`` is present the UI labels the row as manually forced and still shows what the
    engine thought (ARCHITECTURE §4.2). A client that renders one instead of the other destroys
    the only evidence that somebody argued with the ranking.

    Absent values are ``null`` and mean absent — a missing ``target_date`` is the
    ``NO_TARGET_DATE`` signal itself, and substituting today's date would erase a real risk.

    ``project_type`` and ``stage`` travel as bare codes rather than as
    :class:`~apps.shared.refs.TaxonomyRef`, because the read model denormalizes a label only for
    the fields the queue actually renders as text. The project detail carries the full references.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    name: str
    client_alias: str
    owner: ActorRef | None = None
    engagement_type: TaxonomyRef
    project_type_code: str | None = None
    stage_code: str | None = None
    state: StateRef
    health: HealthRef
    target_date: date | None = None
    business_value: float | None = None
    currency: str = "USD"
    next_step: str | None = None
    open_tasks: int = Field(default=0, ge=0)
    overdue_tasks: int = Field(default=0, ge=0)
    blocked_tasks: int = Field(default=0, ge=0)
    open_blockers: int = Field(default=0, ge=0)
    score: ScoreView
    override: QueueOverrideView | None = None
    risk_flags: tuple[RiskFlagView, ...] = ()
    updated_at: datetime


class ProjectDetailView(BaseModel):
    """One project in full, read from the write side.

    ``transitions`` is the **only** source of transition buttons (`docs/API.md` §2.2). The frontend
    holds no list of state codes and never guesses legality, which is what makes adding a workflow
    state a fixture row with zero frontend changes. An empty list is a legitimate answer: it means
    the project sits in a terminal state, and the UI renders no buttons rather than inventing one.

    ``score`` is ``None`` only before the engine has ever run for this project — a freshly created
    one, between its ``project.created`` event and the recalculator's first pass. It is not
    flattened to zero, because "not scored yet" and "scored zero" rank the same and mean opposite
    things.

    ``workflow`` names the graph that ``state`` and ``transitions`` come from, and whether an ops
    lead assigned it to this project or it was inherited from the engagement type's binding. Two
    projects of the same type may now follow different lifecycles, so "why does this one have a
    button the other does not" is a question the payload has to be able to answer.

    ``notes`` travels inside the detail for the reason
    :class:`~apps.work.domain.views.TaskDetailView` already states: a surface that needs two
    requests to render is two chances to render half a page, and two reads can straddle a write, so
    a comment fetched separately can end up rendered beside a state it does not refer to. Newest
    first, capped, and **including the notes written against this project's tasks** — the column is
    copied onto them at write time and the ``note.added`` envelope names the project either way, so
    excluding them would make a note vanish on reload after having appeared live. Empty means the
    project has no commentary, never "we could not read it".
    """

    model_config = ConfigDict(frozen=True)

    code: str
    name: str
    summary: str | None = None
    #: Long-form Markdown; ``""`` when nobody wrote one.
    description: str = ""
    client: TaxonomyRef
    owner: ActorRef | None = None
    engagement_type: TaxonomyRef
    project_type: TaxonomyRef | None = None
    stage: TaxonomyRef | None = None
    state: StateRef
    workflow: WorkflowRef
    health: HealthRef
    start_date: date | None = None
    target_date: date | None = None
    business_value: float | None = None
    currency: str = "USD"
    next_step: str | None = None
    is_archived: bool = False
    open_tasks: int = Field(default=0, ge=0)
    overdue_tasks: int = Field(default=0, ge=0)
    blocked_tasks: int = Field(default=0, ge=0)
    open_blockers: int = Field(default=0, ge=0)
    score: ScoreView | None = None
    override: OverrideView | None = None
    risk_flags: tuple[RiskFlagView, ...] = ()
    tasks: tuple[TaskView, ...] = ()
    blockers: tuple[BlockerView, ...] = ()
    transitions: tuple[TransitionOption, ...] = ()
    notes: tuple[NoteView, ...] = ()
    updated_at: datetime


class TeamLoadView(BaseModel):
    """What one person is carrying right now, computed from task rows at read time.

    The source ``Team`` sheet's counters are a stale projection of the same rows and are
    deliberately not imported (ARCHITECTURE §10): storing them would let the roster and the work
    disagree, and the disagreement would be invisible.

    ``is_overloaded`` never lowers a project's score. It raises ``OWNER_OVERLOADED`` on that
    person's projects, because being short-staffed is a staffing decision and not a reason for the
    work itself to matter less (ARCHITECTURE §4.1).

    ``role`` is the label and ``role_code`` the slug, and both travel because the row is both read
    and edited from the same screen: text renders from the label, while the edit dialog has to send
    a code back and must never map the Spanish label onto one itself (CLAUDE.md rule 1). They are
    two fields rather than a :class:`~apps.shared.refs.TaxonomyRef` only because this shape
    predates the roster being editable and widening it is not a breaking change, while replacing
    ``role`` would be.

    ``is_active`` and ``has_password`` are identity facts served here rather than through a second
    request, because the table renders them in columns beside the load: who is retired, and who is
    a real assignee that cannot sign in yet.
    """

    model_config = ConfigDict(frozen=True)

    alias: str
    label: str
    role: str | None = None
    role_code: str | None = None
    is_active: bool = True
    has_password: bool = False
    weekly_capacity_points: int = Field(gt=0)
    load_points: int = Field(ge=0)
    utilization: float = Field(ge=0)
    is_overloaded: bool = False
    open_tasks: int = Field(default=0, ge=0)
    blocked_tasks: int = Field(default=0, ge=0)
    high_or_critical_open: int = Field(default=0, ge=0)
    overdue_tasks: int = Field(default=0, ge=0)
    projects_owned: int = Field(default=0, ge=0)


class TeamLoadPage(BaseModel):
    """The whole roster's load, as ``GET /api/v1/team/load`` returns it.

    An ``{items: [...]}`` object rather than a bare array, so the endpoint can grow a sibling key —
    a portfolio total, a generated-at instant — without becoming a breaking change for a client
    that parsed the top level as a list. It carries no ``count``: this is the whole roster, and a
    total beside a complete list is a number that can only ever be redundant or wrong.
    """

    model_config = ConfigDict(frozen=True)

    items: tuple[TeamLoadView, ...] = ()


class ClientDirectoryView(BaseModel):
    """The counterparties a project can be registered against, as ``GET /api/v1/clients`` returns.

    An ``{items: [...]}`` object rather than a bare array, for the reason
    :class:`TeamLoadPage` gives: a top-level list cannot grow a sibling key without breaking every
    client that parsed it as one.

    Each entry is a :class:`~apps.shared.refs.TaxonomyRef` even though a client is not a taxonomy
    row, because on the wire it answers the same question — which of these do I choose — and a
    fourth reference shape would be one more thing the frontend has to learn for no gain.
    """

    model_config = ConfigDict(frozen=True)

    items: tuple[TaxonomyRef, ...] = ()

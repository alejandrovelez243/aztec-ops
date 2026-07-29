"""What the identity context publishes about a person: the roster row's identity half.

:class:`MemberView` is deliberately *not* the shape ``GET /api/v1/team/load`` returns. That one —
:class:`~apps.portfolio.domain.views.TeamLoadView` — carries the load, which is an aggregate over
``work.Task`` rows keyed by capacity, and ``accounts`` must not learn that ``work.Task`` exists
(ARCHITECTURE §7). So the write routes answer with what they actually changed, and the roster read
answers with the same identity fields plus the numbers the portfolio derived.

The overlap is real and is the cheaper of the two costs. The alternative — having the write routes
return the loaded row — would put a portfolio read inside an accounts router and invert the
dependency the ``TeamMember``/``User`` merge was careful to avoid.

``role`` is a :class:`~apps.shared.refs.TaxonomyRef` rather than a bare label because a form has to
send the ``code`` back: an edit dialog that could only read ``"Delivery"`` would have to map the
Spanish label onto a code itself, which is exactly what CLAUDE.md rule 1 forbids.
"""

from pydantic import BaseModel, ConfigDict, Field

from apps.shared.refs import TaxonomyRef


class MemberView(BaseModel):
    """One person as the roster write routes hand them back.

    ``alias`` is ``accounts.User.code`` and ``label`` is ``accounts.User.alias``, inverted with
    respect to the columns for the reason `docs/API.md` §1.6 fixed on the wire and every other
    person-shaped payload already follows.

    ``has_password`` is a boolean, and the password itself has no representation here or anywhere
    else on the wire. It exists so the roster can show who cannot sign in yet — a seeded person is
    a real assignee with no credential — without the client inferring it from a failed login.

    ``is_ops_lead`` is read-only on this surface. It is ``is_staff``, which also opens ``/admin/``,
    so granting it is a Django admin action rather than a field on a roster form; a product screen
    that could hand out database access would be a privilege-escalation path with a nice layout.
    """

    model_config = ConfigDict(frozen=True)

    alias: str
    label: str
    role: TaxonomyRef | None = None
    weekly_capacity_points: int = Field(gt=0)
    is_active: bool = True
    is_ops_lead: bool = False
    has_password: bool = False

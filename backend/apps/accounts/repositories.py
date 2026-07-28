"""Every named query of the identity context.

Queries live here rather than inline in a service (ARCHITECTURE §7), and they live in
``accounts`` rather than being repeated in ``portfolio`` and ``work`` because "resolve a person
by code" is one definition: it was written twice before the merge — as
``portfolio.repositories.TeamMemberRepository.get_by_code`` and
``work.repositories.team_member_by_code`` — and definitions written twice drift.

``role`` is selected on every read. It is rendered wherever a person is rendered, so leaving it
lazy turns a roster listing into one query per row.

What is *not* here: owner load. ``weekly_capacity_points`` is a column of this context but the
numerator comes from ``work.Task``, and ``accounts`` must not learn about work — that query
stays in ``apps.portfolio.repositories``.
"""

from apps.accounts.models import User


def user_by_code(code: str) -> User | None:
    """Return the person with this stable code, or ``None``.

    Inactive people still resolve: deactivation retires someone from new assignment, it does not
    erase the tasks and projects that already name them, and a caller rendering those needs the
    row. A caller that means "someone who can take work" filters on ``is_active`` itself.

    Args:
        code: ``User.code``, e.g. ``"camila.torres"``.

    Returns:
        The person, or ``None`` when no row carries that code.
    """
    return User.objects.select_related("role").filter(code=code).first()


def users_by_codes(codes: list[str]) -> dict[str, User]:
    """Resolve many people in one query, keyed by code.

    Exists so a caller holding N codes — a projection builder, a bulk import — does not issue N
    queries. Codes matching no row are simply absent from the mapping rather than mapped to
    ``None``: absence is the answer, and a key present with a null value would invite callers to
    treat "unknown person" as "person with no data".

    Args:
        codes: ``User.code`` values. An empty list returns ``{}`` without querying.

    Returns:
        Mapping of code to person, containing only the codes that exist.
    """
    if not codes:
        return {}
    return {user.code: user for user in User.objects.select_related("role").filter(code__in=codes)}


def active_members() -> list[User]:
    """Return every active person, in alias order.

    This is the assignable roster: ``is_active`` is the single flag for "still part of the
    operation", inherited from ``AbstractUser``. Ordering is by ``alias`` because the result is
    rendered for a human choosing a name, and the list is materialized here so the query cannot
    escape this module.

    Returns:
        Active people ordered by display alias.
    """
    return list(User.objects.select_related("role").filter(is_active=True).order_by("alias"))

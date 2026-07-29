"""Request bodies of the identity routes: the three authentication ones and the three roster ones.

Only inputs live here. The responses are the context's own value objects
(:mod:`apps.accounts.domain.value_objects`, :mod:`apps.accounts.domain.views`) returned straight to
the wire — django-ninja schemas *are* Pydantic models (CLAUDE.md rule 10), so restating
``access``/``refresh``/``actor`` in a parallel ``*Out`` class would buy nothing but a second place
to forget a field.

The roster bodies are one per use case and never one shape with everything optional. ``POST``
cannot accept ``is_active`` because a person is registered as part of the operation or not at all,
``PATCH`` cannot accept ``password`` because replacing a credential has its own route and its own
audit record, and neither accepts ``is_ops_lead`` — that flag is ``is_staff``, which also opens
``/admin/``, so it is granted from Django's own admin rather than from a roster form.
"""

from pydantic import BaseModel, ConfigDict, Field

from apps.accounts.domain.commands import (
    CODE_MAX_LENGTH,
    LABEL_MAX_LENGTH,
    MAX_WEEKLY_CAPACITY_POINTS,
    MIN_WEEKLY_CAPACITY_POINTS,
)


class CredentialsIn(BaseModel):
    """Body of ``POST /api/v1/auth/token``.

    ``min_length=1`` on both, so an empty string is a 422 naming the field rather than a 401 that
    tells the operator their password is wrong when they simply did not type one.
    """

    model_config = ConfigDict(frozen=True)

    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1)


class RefreshIn(BaseModel):
    """Body of ``POST /api/v1/auth/token/refresh``.

    The refresh token travels in the body and never as a cookie: it is the long-lived credential,
    ``EventSource`` has no use for it, and keeping it out of the browser's automatic request path is
    what bounds the damage the short-lived access cookie can do.
    """

    model_config = ConfigDict(frozen=True)

    refresh: str = Field(min_length=1)


class MemberCreateIn(BaseModel):
    """Body of ``POST /api/v1/team/members``.

    ``weekly_capacity_points`` is required rather than defaulted here even though the column has a
    default. A capacity is the divisor of owner load and therefore decides who shows as
    overloaded; a form that could omit it would quietly staff everybody at the same number, and the
    operator would never be shown the value they did not choose.

    ``password`` is optional and omitting it is the normal case: the person exists as an assignee
    immediately and cannot sign in until somebody sets one, which is the state every seeded person
    is already in.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=CODE_MAX_LENGTH)
    label: str = Field(min_length=1, max_length=LABEL_MAX_LENGTH)
    role: str | None = None
    weekly_capacity_points: int = Field(
        ge=MIN_WEEKLY_CAPACITY_POINTS, le=MAX_WEEKLY_CAPACITY_POINTS
    )
    password: str | None = Field(default=None, min_length=1)


class MemberUpdateIn(BaseModel):
    """Body of ``PATCH /api/v1/team/members/{code}``.

    Every field is optional, and "absent" and "``null``" mean different things: an absent ``role``
    is left untouched, an explicitly ``null`` one unclassifies the person. The router reads which
    fields were actually sent from ``model_fields_set`` and carries that set into the command, so
    the distinction survives to the service — without it, "do not touch the role" and "clear the
    role" are the same request.

    ``code`` is absent because it is the address: renaming the slug would orphan every event and
    activity record that already names it. A person whose name changed gets a new ``label``.
    """

    model_config = ConfigDict(frozen=True)

    label: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX_LENGTH)
    role: str | None = None
    weekly_capacity_points: int | None = Field(
        default=None, ge=MIN_WEEKLY_CAPACITY_POINTS, le=MAX_WEEKLY_CAPACITY_POINTS
    )
    is_active: bool | None = None


class MemberPasswordIn(BaseModel):
    """Body of ``POST /api/v1/team/members/{code}/password``.

    No current password: this is an ops lead resetting somebody else's credential, so there is
    none to verify. Strength is not checked here — ``AUTH_PASSWORD_VALIDATORS`` is the policy and
    the service applies it, so a rule configured for the deployment cannot be contradicted by a
    ``min_length`` written into a schema.
    """

    model_config = ConfigDict(frozen=True)

    password: str = Field(min_length=1)

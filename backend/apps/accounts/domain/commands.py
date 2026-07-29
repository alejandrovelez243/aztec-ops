"""What a caller asks the roster use cases to do.

Three commands, one per use case, and none of them is a bag of optional fields standing in for all
three. ``POST`` cannot accept ``is_active`` — a person is registered as part of the operation or
not registered at all — and ``PATCH`` cannot accept ``password``, because replacing a credential is
an action with its own audit record and its own authorization, not a field that rides along with a
capacity edit. Fusing them would make both of those facts runtime checks instead of type errors.

The commands are addressed by ``accounts.User.code``, never by primary key, for the reason every
other context is: the code is what the events carry and what an operator recognises.

Pure: no Django, so a command can be built and asserted on ``SimpleTestCase``.
"""

from pydantic import BaseModel, ConfigDict, Field

#: Length of the stable slug, mirroring ``accounts_user.code`` so an over-long value fails at
#: construction with the field name rather than as a database error mid-transaction.
CODE_MAX_LENGTH = 32

#: Length of the operator-editable display name, mirroring ``accounts_user.alias``.
LABEL_MAX_LENGTH = 96

#: Floor of the weekly capacity. Zero is refused by a database constraint (DATA_MODEL §9.2) and
#: refused here first, because capacity is the *divisor* of owner load: a zero would not be a bad
#: number, it would be a division by zero on every roster read.
MIN_WEEKLY_CAPACITY_POINTS = 1

#: Ceiling of the weekly capacity. Not a business rule — it is a typo catcher. A capacity of 2000
#: silently means "this person can never be overloaded", which disables the ``OWNER_OVERLOADED``
#: flag for them without anything on screen saying so.
MAX_WEEKLY_CAPACITY_POINTS = 200


class CreateMemberCommand(BaseModel):
    """Register a person on the roster.

    ``password`` is optional and absent is the normal case: seeded people have no credentials
    (``set_unusable_password``), and somebody who is assigned work before they are onboarded is a
    real state, not an incomplete row. An account with no usable password simply cannot sign in
    until one is set through the password use case.

    Attributes:
        code: The stable slug every payload will name this person by, e.g. ``"camila.torres"``.
        label: Display name, operator-editable.
        role_code: ``catalog.Role.code``, or ``None`` for an unclassified person.
        weekly_capacity_points: The denominator of owner load.
        password: Optional initial credential.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=CODE_MAX_LENGTH)
    label: str = Field(min_length=1, max_length=LABEL_MAX_LENGTH)
    role_code: str | None = None
    weekly_capacity_points: int = Field(
        ge=MIN_WEEKLY_CAPACITY_POINTS, le=MAX_WEEKLY_CAPACITY_POINTS
    )
    password: str | None = None


class UpdateMemberCommand(BaseModel):
    """Edit a person's mutable attributes, touching only the fields the caller actually sent.

    Every field is optional and **absent is not the same as ``None``**: an absent ``role_code``
    leaves the role alone, an explicit ``None`` clears it. The service reads
    ``model_fields_set`` to tell the two apart, which is the whole reason this is a model and not a
    dict — with a dict, "do not touch the role" and "unclassify this person" are the same request.

    ``code`` is the address, not a field: renaming the slug would orphan every event and activity
    record that already names it. The display name is what changes when somebody's name changes.

    Attributes:
        code: Who is being edited.
        label: New display name.
        role_code: New role, or ``None`` to unclassify.
        weekly_capacity_points: New capacity; changing it can raise or clear ``OWNER_OVERLOADED``
            on every project this person owns, which is why the change is audited.
        is_active: ``False`` retires the person from new assignment; ``True`` restores them.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=CODE_MAX_LENGTH)
    label: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX_LENGTH)
    role_code: str | None = None
    weekly_capacity_points: int | None = Field(
        default=None, ge=MIN_WEEKLY_CAPACITY_POINTS, le=MAX_WEEKLY_CAPACITY_POINTS
    )
    is_active: bool | None = None


class SetMemberPasswordCommand(BaseModel):
    """Replace a person's password.

    A command of its own, not a field of :class:`UpdateMemberCommand`, because the two differ in
    every respect that matters: this one is the only write in the codebase that must never appear
    in a log, an event payload or an activity record's ``to_value``, and it is the only one whose
    *absence of an old value* is deliberate — this is an operator resetting a credential on
    somebody's behalf, so there is no current password to verify.

    Attributes:
        code: Whose credential is being replaced.
        password: The new plaintext, validated against Django's configured validators by the
            service and hashed before it touches the database.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=CODE_MAX_LENGTH)
    password: str = Field(min_length=1)

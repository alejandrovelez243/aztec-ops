"""What a caller asks the catalog's two write use cases to do.

Roles, and only roles. The other five taxonomies are still admin-only, and that asymmetry is
deliberate rather than an omission waiting to be filled: a role is the one taxonomy an operator
needs while doing something else — registering a person who does a job nobody has typed yet — and
making them leave for ``/admin/`` mid-form is how a person ends up filed under the wrong role
forever. An engagement type or a workflow stage is a decision about how the business works, and it
is made deliberately, in the admin, not in passing.

Pure: no Django, so a command can be built and asserted on ``SimpleTestCase``.
"""

from pydantic import BaseModel, ConfigDict, Field

#: Length of the stable slug, mirroring ``catalog_role.code``.
CODE_MAX_LENGTH = 32

#: Length of the operator-editable Spanish label, mirroring ``catalog_role.label``.
LABEL_MAX_LENGTH = 64


class CreateRoleCommand(BaseModel):
    """Add a role to the vocabulary.

    ``code`` is what every payload and every filter compares against and is permanent; ``label``
    is the Spanish an operator reads and may fix at any time. Getting these the wrong way round is
    the failure CLAUDE.md rule 1 exists to prevent, which is why they are two fields and not one
    slugified from the other.

    Attributes:
        code: Stable ASCII slug, e.g. ``"design"``.
        label: Display text, e.g. ``"Diseño"``.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=CODE_MAX_LENGTH)
    label: str = Field(min_length=1, max_length=LABEL_MAX_LENGTH)


class UpdateRoleCommand(BaseModel):
    """Rename a role, or retire and restore it.

    ``code`` is the address and never a field: it is what the people already classified under this
    role carry, so changing it would silently unclassify them.

    Retiring is ``is_active = False`` and never a delete. A retired role disappears from the
    pickers and keeps resolving on the people who already point at it — deleting the row would
    either cascade those people's classification away or fail on the foreign key.

    Attributes:
        code: Which role is being edited.
        label: New display text.
        is_active: ``False`` takes it out of the pickers; ``True`` puts it back.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=CODE_MAX_LENGTH)
    label: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX_LENGTH)
    is_active: bool | None = None

"""Request bodies of the two role-writing routes.

Only inputs live here. Both routes answer with :class:`~apps.shared.refs.TaxonomyRef`, the same
shape every picker already renders, so there is no ``*Out`` class restating three fields.
"""

from pydantic import BaseModel, ConfigDict, Field

from apps.catalog.domain.commands import CODE_MAX_LENGTH, LABEL_MAX_LENGTH


class RoleCreateIn(BaseModel):
    """Body of ``POST /api/v1/catalog/roles``.

    Two fields and not one: ``code`` is the ASCII slug every payload and filter compares against
    and it never changes, ``label`` is the Spanish an operator reads and may fix at any time.
    Slugifying the label into a code instead would tie the identifier to the wording, and the
    first typo fix would silently unclassify everybody (CLAUDE.md rule 1).

    ``order`` is absent: a new role goes to the end of the picker, and rearranging the vocabulary
    is a deliberate act done in the admin rather than a number typed into a form that was really
    about registering a person.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=CODE_MAX_LENGTH)
    label: str = Field(min_length=1, max_length=LABEL_MAX_LENGTH)


class RoleUpdateIn(BaseModel):
    """Body of ``PATCH /api/v1/catalog/roles/{code}``.

    Both fields optional, and absent means untouched. ``code`` is not among them: it is the
    address, and the people already classified under this role carry it.
    """

    model_config = ConfigDict(frozen=True)

    label: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX_LENGTH)
    is_active: bool | None = None

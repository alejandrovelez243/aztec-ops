"""Request bodies of the three authentication routes.

Only inputs live here. The responses are the context's own value objects
(:mod:`apps.accounts.domain.value_objects`) returned straight to the wire — django-ninja schemas
*are* Pydantic models (CLAUDE.md rule 10), so restating ``access``/``refresh``/``actor`` in a
parallel ``*Out`` class would buy nothing but a second place to forget a field.
"""

from pydantic import BaseModel, ConfigDict, Field


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

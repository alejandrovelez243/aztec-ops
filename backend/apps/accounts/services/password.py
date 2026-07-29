"""The one place a plaintext password is turned into a stored hash.

Private to the identity context's use cases and shared by two of them, because "validate against
the configured validators, then hash" written twice is how a second call site ends up hashing
something the first would have refused.

Django's validator list is the policy, not a constant here: ``AUTH_PASSWORD_VALIDATORS`` in
``config/settings`` is where minimum length and the common-password blocklist are configured, and a
service that re-checked a rule of its own would be a second opinion nobody updates.

**Nothing in this module logs, returns or re-raises the plaintext.** The rejection carries the
validators' complaints, which describe the rule that was broken and never the value that broke it.
"""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from apps.accounts.domain.errors import PasswordRejected
from apps.accounts.models import User


def hash_password_onto(member: User, password: str | None) -> None:
    """Set the person's password hash in memory, or make the account unreachable.

    Mutates ``member`` and does not save: the caller owns the transaction and the write, so this
    cannot half-apply a change whose surrounding activity record was rolled back.

    ``None`` means "no credential", and it is a legitimate value rather than a missing one. It sets
    an *unusable* hash — a sentinel that matches no input, not an empty password — which is the
    state every seeded person is in: a real assignee who cannot sign in until somebody sets one.

    Args:
        member: The person to set the hash on, saved or unsaved.
        password: The new plaintext, or ``None`` for an account with no usable credential.

    Raises:
        PasswordRejected: The password did not survive ``AUTH_PASSWORD_VALIDATORS``. Carries every
            complaint, not the first: a form that fixes one rule per round trip is a form people
            work around by choosing something worse.
    """
    if password is None:
        member.set_unusable_password()
        return
    try:
        # ``user=member`` is what lets the similarity validator compare against the code, the alias
        # and the email — the three attributes an operator is most likely to reuse as a password.
        validate_password(password, user=member)
    except ValidationError as error:
        raise PasswordRejected(tuple(error.messages)) from error
    member.set_password(password)

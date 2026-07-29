"""Use case: realign the business-code sequences with the codes already stored.

This exists because of one sharp edge in Django. ``loaddata`` deserializes into
``Model.save_base(raw=True)`` and therefore **never calls** ``Model.save()`` — which is exactly
where ``Blocker.code`` and ``Note.code`` are drawn from their PostgreSQL sequences. The fixtures
consequently carry their codes explicitly, and the sequences, untouched by the load, still sit at
1. The seed then looks entirely successful and the *first* blocker a user raises collides on
``work_blocker_code_unique``, hours later and far from the cause.

It used to be ``manage.py sync_code_sequences``, and that was the wrong packaging: a step someone
has to remember to run is a step someone forgets. It is now a service that ``manage.py seed``
calls unconditionally, immediately after ``loaddata``, so the repair happens in the same breath as
the damage.

Ends at the database, writes no ``ActivityRecord`` and no ``OutboxEvent``: a sequence is
infrastructure, not a business fact, and nothing downstream reacts to it.
"""

from typing import Final

from django.db import connection
from pydantic import BaseModel, ConfigDict

from apps.work.models import (
    BLOCKER_CODE_PREFIX,
    BLOCKER_CODE_SEQUENCE,
    NOTE_CODE_PREFIX,
    NOTE_CODE_SEQUENCE,
    Blocker,
    Note,
)

#: The value a sequence is set to when its table is empty. ``setval(seq, 1, false)`` makes the
#: next ``nextval`` return 1 — the ``is_called = false`` form, so an empty table does not burn
#: the first code.
EMPTY_TABLE_START: Final = 1


class SequenceSync(BaseModel):
    """What one sequence was moved to, in the terms a seed log has to be readable in.

    ``next_code`` is rendered rather than derived by the reader because it is the value the
    collision would have been about, and a log that states it makes a broken sync obvious at a
    glance instead of requiring arithmetic.
    """

    model_config = ConfigDict(frozen=True)

    sequence: str
    highest: int
    next_code: str


def sync_code_sequences() -> tuple[SequenceSync, ...]:
    """Advance ``work_blocker_code_seq`` and ``work_note_code_seq`` past the stored codes.

    Idempotent and safe to run at any time, including on a database that was never seeded: the
    high-water mark is read from the rows themselves, so running it twice is a no-op and running
    it after real traffic cannot renumber anything. Codes are never reused and never renumbered;
    this only guarantees the next one is free.

    Returns:
        One entry per sequence, in a fixed order, describing where it now points.

    Raises:
        DatabaseError: The sequence named in ``apps.work.models`` does not exist — the tables have
            not been migrated, and seeding against them would fail later and less clearly.
    """
    syncs: list[SequenceSync] = []
    for model, sequence, prefix in (
        (Blocker, BLOCKER_CODE_SEQUENCE, BLOCKER_CODE_PREFIX),
        (Note, NOTE_CODE_SEQUENCE, NOTE_CODE_PREFIX),
    ):
        highest = _highest_code_number(model, prefix)
        _setval(sequence, highest)
        syncs.append(
            SequenceSync(
                sequence=sequence,
                highest=highest,
                next_code=f"{prefix}-{highest + 1:04d}",
            )
        )
    return tuple(syncs)


def _highest_code_number(model: type[Blocker] | type[Note], prefix: str) -> int:
    """The largest numeric suffix among the codes stored in one table.

    ``max()`` over the *string* is correct only because every code is zero-padded to the same
    width; past four digits the padding is a floor rather than a limit, so the suffix is parsed
    rather than compared as text.

    Args:
        model: ``Blocker`` or ``Note``.
        prefix: ``"BLK"`` or ``"NOTE"``, stripped before parsing.

    Returns:
        The highest number seen, or 0 when the table is empty.
    """
    codes = model.objects.exclude(code="").values_list("code", flat=True)
    numbers = [int(code.removeprefix(f"{prefix}-")) for code in codes if code.startswith(prefix)]
    return max(numbers, default=0)


def _setval(sequence: str, highest: int) -> None:
    """Point a sequence at ``highest`` so the next ``nextval`` returns ``highest + 1``.

    Args:
        sequence: The sequence name, taken from ``apps.work.models`` rather than retyped, so a
            rename cannot leave this service silently pointing at nothing.
        highest: The largest code number already stored; 0 for an empty table.
    """
    with connection.cursor() as cursor:
        if highest == 0:
            cursor.execute("SELECT setval(%s, %s, false)", [sequence, EMPTY_TABLE_START])
            return
        cursor.execute("SELECT setval(%s, %s, true)", [sequence, highest])

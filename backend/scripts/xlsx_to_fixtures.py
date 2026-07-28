#!/usr/bin/env python
"""Turn the normalized challenge dataset into the committed Django fixtures.

Developer tooling. **Nothing on the runtime path imports this module** — the runtime only ever
sees the JSON it writes, loaded with ``manage.py loaddata`` (CLAUDE.md rule 9). The source
spreadsheet is gitignored; the fixtures are the shareable, reviewable form of the same facts.

Run it from ``backend/``::

    uv run --project backend python scripts/xlsx_to_fixtures.py

Two properties are load-bearing and are the reason this file exists rather than a one-off
notebook:

* **Every emitted object carries an explicit ``pk``.** ``loaddata`` is then an upsert, so running
  the seed twice leaves the database byte-identical instead of doubling every table.
* **Every ``Blocker`` and ``Note`` carries an explicit ``code``.** ``loaddata`` writes through
  ``save_base(raw=True)`` and therefore never calls ``Model.save()``, where those codes are drawn
  from their PostgreSQL sequence. A fixture row without a code would insert an empty string and
  collide on the unique constraint. The sequences must afterwards be advanced past the seeded
  maximum, which is what ``manage.py sync_code_sequences`` does — see the seed command.

What is deliberately *not* imported: the ``Team`` sheet counters. They are a stale projection of
the task rows; load is recomputed by ``portfolio.repositories.owner_load_for_codes`` (DATA_MODEL
§3). Importing them would ship a number that is wrong the moment a task moves.

What is deliberately *authored* rather than sourced is listed in :data:`AUTHORED_PROJECT_STATES`
and :data:`ACTIVITY_ANCHOR`; both carry their justification inline.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, TypedDict
from uuid import NAMESPACE_URL, uuid5

from django.contrib.auth.hashers import make_password

# --------------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------------

#: ``backend/``.
BACKEND_ROOT: Final = Path(__file__).resolve().parent.parent

#: Repository root; the raw dataset lives outside ``backend/`` because it is not Python.
REPO_ROOT: Final = BACKEND_ROOT.parent

#: The normalized dataset. Gitignored: it is the challenge's own data, not ours to redistribute.
DATASET_PATH: Final = REPO_ROOT / "data" / "raw" / "dataset.json"

#: ``apps/``; every fixture is written to ``apps/<app>/fixtures/<app>.json``.
APPS_ROOT: Final = BACKEND_ROOT / "apps"


# --------------------------------------------------------------------------------------------
# Reference instants
# --------------------------------------------------------------------------------------------

#: The day the source spreadsheet was exported, inferred from the data rather than guessed: the
#: latest ``due_date`` still flagged ``is_overdue = "Si"`` is 2026-07-12, so the export happened on
#: the day after. Used only as the anchor for the authored activity trail below.
DATASET_REFERENCE_DATE: Final = date(2026, 7, 13)

#: Noon UTC on the reference date. The seeded ``ActivityRecord`` rows fan out backwards from here.
#:
#: **Authored, not sourced.** The dataset carries no activity timestamps at all, and
#: ``last_activity_at`` is what the ``staleness`` signal and the ``IsStale`` specification read.
#: Leaving it absent would mark all 22 projects maximally stale and flatten that signal into a
#: constant, which would hide exactly the differences the board exists to show. A deterministic
#: spread is authored demo data; it is never presented as a fact from the spreadsheet.
ACTIVITY_ANCHOR: Final = datetime.combine(
    DATASET_REFERENCE_DATE, datetime.min.time(), tzinfo=UTC
) + timedelta(hours=12)

#: How many distinct staleness ages the authored activity trail spans. Twelve days straddles the
#: 14-day default ``STALENESS_THRESHOLD_DAYS`` closely enough that both sides of it are populated.
ACTIVITY_SPREAD_DAYS: Final = 12

#: The instant every configuration row and every person is dated from.
#:
#: **Every ``auto_now_add`` and ``auto_now`` column must be present in the fixture.** ``loaddata``
#: writes through ``save_base(raw=True)``, and ``_save_table`` skips ``Field.pre_save`` entirely
#: when ``raw`` is set — so the automatic timestamp is *not* applied and the column takes whatever
#: the fixture says, which for a missing key is ``None`` and a ``NOT NULL`` violation. The same
#: mechanism that skips ``Model.save()`` and leaves the code sequences behind skips these too.
SEED_EPOCH: Final = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

#: ``date_joined`` for every seeded user. Pinned so regeneration is a no-op in git.
USER_JOINED_AT: Final = SEED_EPOCH

#: How long before the reference date the task rows are dated. The source has no task timestamps;
#: this only has to be earlier than ``ACTIVITY_ANCHOR`` and stable.
TASK_AGE_DAYS: Final = 30

#: Ages, in days, cycled across the projects that have blockers.
#:
#: **Authored, not sourced.** The source names impediments in prose and dates none of them, yet
#: ``blockage`` scores the *age* of the oldest open blocker and saturates at 40 days: giving every
#: blocker the same timestamp would collapse that signal onto its 0.5 floor for the whole
#: portfolio and make "this one has been rotting for six weeks" unrepresentable. The cycle
#: straddles the saturation point so both ends of the signal are exercised.
BLOCKER_AGE_DAYS_CYCLE: Final[tuple[int, ...]] = (3, 9, 17, 26, 34, 41)

#: One correlation id for the whole seed: loading the fixtures is a single movement, and the
#: audit trail should be reconstructible as one (CLAUDE.md rule 3). Derived, never random, so the
#: file does not churn between runs.
SEED_CORRELATION_ID: Final = uuid5(NAMESPACE_URL, "https://aztec.ops/seed/v1")


# --------------------------------------------------------------------------------------------
# Taxonomies — the distinct values actually present in the source
# --------------------------------------------------------------------------------------------

#: Spanish label -> (code, order, weight, color). The multiplier is the ``engagement_type``
#: modifier of ARCHITECTURE §4.1: a Diagnostico near its deadline outranks recurring maintenance
#: with the same raw signals, which is the whole reason the modifier exists.
ENGAGEMENT_TYPES: Final[tuple[tuple[str, str, int, str, str], ...]] = (
    ("Proyecto", "proyecto", 1, "1.00", "#2563eb"),
    ("Mantenimiento o recurrente", "mantenimiento_recurrente", 2, "0.90", "#0d9488"),
    ("Diagnostico", "diagnostico", 3, "1.10", "#a855f7"),
)

#: Spanish label -> (code, order, color). The source column is ``project_type_api``.
PROJECT_TYPES: Final[tuple[tuple[str, str, int, str], ...]] = (
    ("Automatizacion", "automatizacion", 1, "#0891b2"),
    ("Consultoria", "consultoria", 2, "#7c3aed"),
)

#: Delivery stage. ``order`` is meaningful: the stage is what the project's initial
#: ``WorkflowState`` is derived from, because the source ``status`` is ``Activo`` on all 22 rows.
STAGES: Final[tuple[tuple[str, str, int, str], ...]] = (
    ("Descubrimiento", "descubrimiento", 1, "#f59e0b"),
    ("Ejecucion", "ejecucion", 2, "#2563eb"),
)

#: Task priority -> (code, order, weight, is_urgent, color). ``is_urgent`` is what the
#: ``criticality`` signal counts, so the codes ``critica`` and ``alta`` never appear in Python.
PRIORITIES: Final[tuple[tuple[str, str, int, str, bool, str], ...]] = (
    ("Critica", "critica", 1, "3.00", True, "#dc2626"),
    ("Alta", "alta", 2, "2.00", True, "#ea580c"),
    ("Media", "media", 3, "1.00", False, "#ca8a04"),
    ("Baja", "baja", 4, "0.50", False, "#65a30d"),
)

#: The two roles the source uses, in ``owner_role`` and ``assignee_role``.
ROLES: Final[tuple[tuple[str, str, int, str], ...]] = (
    ("Delivery", "delivery", 1, "#0ea5e9"),
    ("Commercial / Delivery", "commercial_delivery", 2, "#8b5cf6"),
)


# --------------------------------------------------------------------------------------------
# Workflows
# --------------------------------------------------------------------------------------------

#: ``(code, label, category, is_initial, is_terminal, order, color)`` for the project graph.
#:
#: ``pausado`` is ``BACKLOG`` and not ``BLOCKED``: the category vocabulary is closed and every risk
#: specification reads it, so filing a paused project as blocked would raise a ``BLOCKED`` flag for
#: a project nobody is waiting on. "Open but not moving" is what ``BACKLOG`` already means.
PROJECT_STATES: Final[tuple[tuple[str, str, str, bool, bool, int, str], ...]] = (
    ("descubrimiento", "Descubrimiento", "BACKLOG", True, False, 1, "#f59e0b"),
    ("ejecucion", "Ejecucion", "IN_PROGRESS", False, False, 2, "#2563eb"),
    ("pausado", "Pausado", "BACKLOG", False, False, 3, "#64748b"),
    ("bloqueado", "Bloqueado", "BLOCKED", False, False, 4, "#dc2626"),
    ("entregado", "Entregado", "DONE", False, True, 5, "#16a34a"),
    ("cancelado", "Cancelado", "CANCELLED", False, True, 6, "#475569"),
)

#: ``(from_code, to_code, label, requires_reason, requires_fields, order)`` for the project graph.
#: Leaving ``bloqueado`` demands a ``next_step``: a project that comes back from blocked without a
#: stated next move is the exact row that goes quiet again a week later.
PROJECT_TRANSITIONS: Final[tuple[tuple[str, str, str, bool, list[str], int], ...]] = (
    ("descubrimiento", "ejecucion", "Iniciar ejecucion", False, [], 1),
    ("descubrimiento", "cancelado", "Cancelar", True, [], 2),
    ("ejecucion", "bloqueado", "Marcar bloqueado", True, [], 1),
    ("ejecucion", "pausado", "Pausar", True, [], 2),
    ("ejecucion", "entregado", "Marcar entregado", False, [], 3),
    ("ejecucion", "cancelado", "Cancelar", True, [], 4),
    ("bloqueado", "ejecucion", "Desbloquear", True, ["next_step"], 1),
    ("bloqueado", "cancelado", "Cancelar", True, [], 2),
    ("pausado", "ejecucion", "Reanudar", False, ["next_step"], 1),
    ("pausado", "cancelado", "Cancelar", True, [], 2),
)

#: ``(code, label, category, is_initial, is_terminal, order, color)`` for the task graph.
#: ``hecha`` and ``cancelada`` are seeded although the source contains no completed task at all —
#: a workflow that cannot express completion is not a workflow.
TASK_STATES: Final[tuple[tuple[str, str, str, bool, bool, int, str], ...]] = (
    ("por_hacer", "Por hacer", "BACKLOG", True, False, 1, "#94a3b8"),
    ("en_progreso", "En progreso", "IN_PROGRESS", False, False, 2, "#2563eb"),
    ("en_revision", "En revision", "IN_PROGRESS", False, False, 3, "#7c3aed"),
    ("bloqueada", "Bloqueada", "BLOCKED", False, False, 4, "#dc2626"),
    ("hecha", "Hecha", "DONE", False, True, 5, "#16a34a"),
    ("cancelada", "Cancelada", "CANCELLED", False, True, 6, "#475569"),
)

#: ``(from_code, to_code, label, requires_reason, requires_fields, order)`` for the task graph.
TASK_TRANSITIONS: Final[tuple[tuple[str, str, str, bool, list[str], int], ...]] = (
    ("por_hacer", "en_progreso", "Empezar", False, [], 1),
    ("por_hacer", "bloqueada", "Bloquear", True, [], 2),
    ("por_hacer", "cancelada", "Cancelar", True, [], 3),
    ("en_progreso", "en_revision", "Enviar a revision", False, [], 1),
    ("en_progreso", "bloqueada", "Bloquear", True, [], 2),
    ("en_progreso", "por_hacer", "Devolver al backlog", True, [], 3),
    ("en_progreso", "cancelada", "Cancelar", True, [], 4),
    ("en_revision", "hecha", "Aprobar", False, [], 1),
    ("en_revision", "en_progreso", "Pedir cambios", True, [], 2),
    ("en_revision", "bloqueada", "Bloquear", True, [], 3),
    ("bloqueada", "en_progreso", "Desbloquear", True, [], 1),
    ("bloqueada", "cancelada", "Cancelar", True, [], 2),
)

#: Source ``Tasks.status`` -> task ``WorkflowState.code``.
TASK_STATUS_TO_STATE: Final[dict[str, str]] = {
    "Por hacer": "por_hacer",
    "En progreso": "en_progreso",
    "En revision": "en_revision",
    "Bloqueada": "bloqueada",
}

#: Source ``Projects.stage`` -> project ``WorkflowState.code``. Every source row is ``Activo``, so
#: the stage is the only state-bearing column the dataset has.
STAGE_TO_PROJECT_STATE: Final[dict[str, str]] = {
    "Descubrimiento": "descubrimiento",
    "Ejecucion": "ejecucion",
}

#: Project code -> (authored ``WorkflowState.code``, why this row).
#:
#: **Authored, not sourced.** All 22 source rows are ``status = "Activo"``, so the data cannot
#: produce a paused, blocked, delivered or cancelled project, and the challenge explicitly asks
#: for a board that shows projects in different states. Each choice below is anchored in something
#: the row already says, so the authored state is a plausible reading of the same facts rather
#: than a random sprinkle. Everything not listed here takes its state from ``stage``.
AUTHORED_PROJECT_STATES: Final[dict[str, tuple[str, str]]] = {
    "PRJ-01": (
        "bloqueado",
        "health Bloqueado, a task in Bloqueada, external-dependency blockers and no target date",
    ),
    "PRJ-22": (
        "bloqueado",
        "health Bloqueado with a target date three months past and a task in Bloqueada",
    ),
    "PRJ-17": (
        "pausado",
        "healthy recurring maintenance whose target date is still months out; nothing is moving",
    ),
    "PRJ-21": (
        "entregado",
        "zero tasks in the backlog and a target date five months past: the work is finished",
    ),
    "PRJ-16": (
        "cancelado",
        "the smallest Diagnostico, at risk, with no target date: the engagement was called off",
    ),
}


# --------------------------------------------------------------------------------------------
# Blocker classification
# --------------------------------------------------------------------------------------------

#: ``BlockerKind`` -> the lowercase substrings that imply it, in the order kinds are emitted.
#:
#: The source ``blockers`` column is one English paragraph per project. It is split into
#: sentences, and a sentence yields **one blocker per kind it matches** — "There are external
#: dependencies or pending accesses." names two different impediments and collapsing it to one
#: would throw away half of what the operator wrote. A sentence matching nothing falls back to
#: ``TECHNICAL`` rather than being dropped.
BLOCKER_KEYWORDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "EXTERNAL_DEPENDENCY",
        ("external", "dependenc", "vendor", "third party", "waiting on", "provider"),
    ),
    ("ACCESS", ("access", "credential", "permission", "account", "login")),
    (
        "DECISION",
        ("decision", "decide", "approval", "approve", "sign-off", "close path", "scope"),
    ),
    ("TECHNICAL", ("cleanup", "follow-up", "service", "bug", "error", "technical", "performance")),
)

#: The kind assigned to a blocker sentence that matches no keyword at all.
BLOCKER_FALLBACK_KIND: Final = "TECHNICAL"

#: The ``recent_completed_examples`` value that means "there is nothing to report". It becomes no
#: note: an empty note is noise in a timeline.
NO_COMPLETED_EXAMPLES: Final = "No completed examples visible"


# --------------------------------------------------------------------------------------------
# Prioritization policy
# --------------------------------------------------------------------------------------------

#: The weights of ARCHITECTURE §4.1, which must sum to 1.0 and cover the signal registry exactly —
#: ``load_policy`` refuses the row otherwise, which is the failure mode this table guards against.
POLICY_WEIGHTS: Final[dict[str, float]] = {
    "deadline_pressure": 0.25,
    "overdue_work": 0.20,
    "criticality": 0.15,
    "business_value": 0.15,
    "blockage": 0.15,
    "staleness": 0.10,
}

#: Modifiers the policy applies to the weighted sum. Owner saturation is deliberately absent: it
#: raises a flag, it never lowers a score (ARCHITECTURE §4.1).
POLICY_MODIFIERS: Final[dict[str, bool]] = {"engagement_type": True}

#: Version string copied into every score computed with this policy.
POLICY_VERSION: Final = "v1"


class FixtureObject(TypedDict):
    """One serialized Django object, in the shape ``loaddata`` expects.

    ``pk`` is always present and always explicit; that is what makes a second ``loaddata`` an
    update of the same row instead of an insert of a duplicate.
    """

    model: str
    pk: int
    fields: dict[str, Any]


def _slug(value: str) -> str:
    """ASCII slug for a Spanish label or an invented alias.

    Accents are folded rather than stripped, so ``Diagnóstico`` and ``Diagnostico`` land on the
    same code and cannot silently create two taxonomy rows for one concept.

    Args:
        value: Any human label.

    Returns:
        Lowercase ASCII with runs of non-alphanumerics collapsed to a single underscore.
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", folded.lower())).strip("_")


def _person_code(alias: str) -> str:
    """Stable ``accounts.User.code`` for a person, e.g. ``Camila Torres`` -> ``camila.torres``.

    The code is also the ``username``, the ``actor`` string on every ``ActivityRecord`` and the
    ``author`` of a note, so it must never change once seeded.

    Args:
        alias: Display name exactly as the source spells it.

    Returns:
        The dotted lowercase ASCII code.
    """
    return _slug(alias).replace("_", ".")


def _iso_date(value: str | None) -> str | None:
    """Normalize a source date cell to ISO ``YYYY-MM-DD``.

    One ``start_date`` in the source is written ``02/03/2026`` while every other date is already
    ISO. It is read as day/month/year, matching the locale the rest of the sheet was written in —
    reading it as month/day would move that project's start into February and, on a different row,
    could invert ``start_date <= target_date`` and make the fixture unloadable.

    Args:
        value: The raw cell, already ``None`` where the spreadsheet said ``'None'``.

    Returns:
        The ISO date, or ``None`` when the cell is empty.
    """
    if not value:
        return None
    if "/" in value:
        day, month, year = value.split("/")
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    return value


def _started_at(start_date: str | None) -> datetime:
    """When a project row came into existence, for its ``created_at``.

    ``created_at`` is ``auto_now_add`` and ``loaddata`` does not apply it, so the fixture has to
    say something. Saying the project's own start date is the only answer the data supports; the
    nine rows with no start date fall back to the seed epoch rather than to "now", which would
    make a nine-month-old engagement look like it was created this morning.

    Args:
        start_date: The raw ``start_date`` cell, possibly ``None`` or non-ISO.

    Returns:
        A timezone-aware instant at 09:00 UTC on the start date, or :data:`SEED_EPOCH`.
    """
    iso = _iso_date(start_date)
    if iso is None:
        return SEED_EPOCH
    return datetime.combine(date.fromisoformat(iso), datetime.min.time(), tzinfo=UTC) + timedelta(
        hours=9
    )


def _decimal_or_none(value: str | None) -> str | None:
    """Cast a source money cell to a fixed-point string, or ``None``.

    Kept as a string rather than a float: ``business_value`` is ``numeric(12,2)`` and a float would
    round 85000000 into something that no longer equals the contract.

    Args:
        value: The raw cell.

    Returns:
        The value with two decimal places, or ``None`` when the cell is empty.
    """
    if not value:
        return None
    return f"{int(float(value))}.00"


def _sentences(paragraph: str) -> list[str]:
    """Split the free-text ``blockers`` paragraph into its sentences.

    Args:
        paragraph: The raw English prose from the source.

    Returns:
        Trimmed sentences with their trailing period restored; empty fragments are dropped.
    """
    parts = [part.strip() for part in paragraph.split(".")]
    return [f"{part}." for part in parts if part]


def _blocker_kinds(sentence: str) -> list[str]:
    """Classify one blocker sentence into the kinds it names.

    A keyword heuristic, and it is honest about being one: the source column is prose, not a typed
    field, and a sentence that names two impediments produces two rows so the operator's own words
    are not silently halved. A sentence that matches nothing is still kept, as ``TECHNICAL`` —
    dropping it would delete an impediment because our vocabulary did not recognize it.

    Args:
        sentence: One sentence of the ``blockers`` paragraph.

    Returns:
        The matched kinds in :data:`BLOCKER_KEYWORDS` order, or the fallback kind.
    """
    lowered = sentence.lower()
    matched = [
        kind for kind, keywords in BLOCKER_KEYWORDS if any(word in lowered for word in keywords)
    ]
    return matched or [BLOCKER_FALLBACK_KIND]


def _dependency_index(tasks: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Index task titles per project so a free-text dependency can be resolved to a task.

    The source writes a dependency as the *title* of another task, but titles carry a
    ``" - <project name>"` suffix that the dependency cell omits. Both forms are indexed, and the
    lookup is scoped to the project: matching across projects would invent an edge between two
    unrelated engagements that happen to share a boilerplate title, and the whole dataset is built
    from boilerplate titles.

    Args:
        tasks: Every source task row.

    Returns:
        ``project_code -> {title form -> task_code}``.
    """
    index: dict[str, dict[str, str]] = defaultdict(dict)
    for task in tasks:
        title = str(task["title"])
        index[task["project_code"]][title] = task["task_code"]
        index[task["project_code"]][title.split(" - ", maxsplit=1)[0].strip()] = task["task_code"]
    return index


def _load_existing_passwords(path: Path) -> dict[int, str]:
    """Read back the unusable password hashes already committed in the accounts fixture.

    ``set_unusable_password()`` draws 40 random characters, so regenerating would rewrite every
    user row and produce a diff that says nothing. Reusing the committed value keeps regeneration
    a no-op in git while every hash still is a genuine, unauthenticatable one.

    Args:
        path: The accounts fixture, which may not exist yet on a first run.

    Returns:
        ``pk -> password``; empty when the file is absent.
    """
    if not path.exists():
        return {}
    objects: list[FixtureObject] = json.loads(path.read_text(encoding="utf-8"))
    return {
        obj["pk"]: str(obj["fields"]["password"])
        for obj in objects
        if obj["model"] == "accounts.user"
    }


def _write(app_label: str, objects: list[FixtureObject]) -> Path:
    """Write one app's fixture, sorted the way it was built and formatted stably.

    Args:
        app_label: The app under ``apps/`` that owns the rows.
        objects: The serialized objects, already in load order.

    Returns:
        The path written, for the run report.
    """
    path = APPS_ROOT / app_label / "fixtures" / f"{app_label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(objects, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def build_catalog() -> tuple[list[FixtureObject], dict[str, dict[str, int]]]:
    """Build the five taxonomy tables.

    Returns:
        The fixture objects and a ``model -> {code -> pk}`` map the later builders resolve
        foreign keys through, so no builder repeats the primary-key arithmetic.
    """
    objects: list[FixtureObject] = []
    ids: dict[str, dict[str, int]] = {
        "engagementtype": {},
        "projecttype": {},
        "stage": {},
        "priority": {},
        "role": {},
    }

    for pk, (label, code, order, weight, color) in enumerate(ENGAGEMENT_TYPES, start=1):
        ids["engagementtype"][label] = pk
        objects.append(
            {
                "model": "catalog.engagementtype",
                "pk": pk,
                "fields": {
                    "code": code,
                    "label": label,
                    "order": order,
                    "is_active": True,
                    "color": color,
                    "weight": weight,
                },
            }
        )

    for pk, (label, code, order, color) in enumerate(PROJECT_TYPES, start=1):
        ids["projecttype"][label] = pk
        objects.append(
            {
                "model": "catalog.projecttype",
                "pk": pk,
                "fields": {
                    "code": code,
                    "label": label,
                    "order": order,
                    "is_active": True,
                    "color": color,
                },
            }
        )

    for pk, (label, code, order, color) in enumerate(STAGES, start=1):
        ids["stage"][label] = pk
        objects.append(
            {
                "model": "catalog.stage",
                "pk": pk,
                "fields": {
                    "code": code,
                    "label": label,
                    "order": order,
                    "is_active": True,
                    "color": color,
                },
            }
        )

    for pk, (label, code, order, weight, is_urgent, color) in enumerate(PRIORITIES, start=1):
        ids["priority"][label] = pk
        objects.append(
            {
                "model": "catalog.priority",
                "pk": pk,
                "fields": {
                    "code": code,
                    "label": label,
                    "order": order,
                    "is_active": True,
                    "color": color,
                    "weight": weight,
                    "is_urgent": is_urgent,
                },
            }
        )

    for pk, (label, code, order, color) in enumerate(ROLES, start=1):
        ids["role"][label] = pk
        objects.append(
            {
                "model": "catalog.role",
                "pk": pk,
                "fields": {
                    "code": code,
                    "label": label,
                    "order": order,
                    "is_active": True,
                    "color": color,
                },
            }
        )

    return objects, ids


def build_accounts(
    dataset: dict[str, list[dict[str, Any]]], role_ids: dict[str, int]
) -> tuple[list[FixtureObject], dict[str, int]]:
    """Build the people, from the union of the ``Team``, ``Projects`` and ``Tasks`` sheets.

    The roster is not the ``Team`` sheet alone: one person owns a project and carries four tasks
    without appearing there, and an assignee the fixture omits would make the task rows unloadable.
    Roles are taken from ``owner_role`` / ``assignee_role`` where the ``Team`` sheet is silent.

    The ``Team`` counters are read for nobody: they are a projection that is stale the moment a
    task moves, and load is recomputed from ``work_task`` (DATA_MODEL §3).

    Args:
        dataset: The parsed source workbook.
        role_ids: ``catalog.Role`` label -> pk.

    Returns:
        The user objects and ``alias -> pk``.
    """
    roles: dict[str, str] = {}
    for member in dataset["Team"]:
        roles[member["member_alias"]] = member["role"]
    for project in dataset["Projects"]:
        roles.setdefault(project["owner_alias"], project["owner_role"])
    for task in dataset["Tasks"]:
        roles.setdefault(task["assignee_alias"], task["assignee_role"])

    existing = _load_existing_passwords(APPS_ROOT / "accounts" / "fixtures" / "accounts.json")
    joined_at = USER_JOINED_AT.isoformat()

    objects: list[FixtureObject] = []
    ids: dict[str, int] = {}
    for pk, alias in enumerate(sorted(roles), start=1):
        ids[alias] = pk
        code = _person_code(alias)
        given, _, family = alias.partition(" ")
        objects.append(
            {
                "model": "accounts.user",
                "pk": pk,
                "fields": {
                    # A genuine unusable hash: these people are real assignees who cannot yet sign
                    # in. Reused across runs so regeneration does not churn the file.
                    "password": existing.get(pk) or make_password(None),
                    "last_login": None,
                    "is_superuser": False,
                    "username": code,
                    "first_name": given,
                    "last_name": family,
                    "email": "",
                    "is_staff": False,
                    "is_active": True,
                    "date_joined": joined_at,
                    "code": code,
                    "alias": alias,
                    "role": role_ids[roles[alias]],
                    "weekly_capacity_points": 20,
                    "groups": [],
                    "user_permissions": [],
                },
            }
        )
    return objects, ids


def build_workflow(
    engagement_type_ids: dict[str, int],
) -> tuple[list[FixtureObject], dict[str, dict[str, int]]]:
    """Build the two state graphs, their states, their edges and their default bindings.

    States the dataset never reaches — ``pausado``, ``entregado``, ``cancelado``, ``hecha``,
    ``cancelada`` — are seeded anyway. A graph that only contains the states the current data
    happens to occupy cannot be moved through, and the transition service would have nothing to
    validate against.

    Args:
        engagement_type_ids: Reserved for engagement-specific bindings; the seed ships only the
            two per-kind defaults, so no engagement type is bound to a workflow of its own yet.

    Returns:
        The fixture objects and ``{"PROJECT": {state_code: pk}, "TASK": {...}}``.
    """
    _ = engagement_type_ids
    objects: list[FixtureObject] = [
        {
            "model": "workflow.workflow",
            "pk": 1,
            "fields": {
                "code": "project_default",
                "name": "Ciclo de vida de proyecto",
                "applies_to": "PROJECT",
                "is_default": True,
                "is_active": True,
                "created_at": SEED_EPOCH.isoformat(),
            },
        },
        {
            "model": "workflow.workflow",
            "pk": 2,
            "fields": {
                "code": "task_default",
                "name": "Ciclo de vida de tarea",
                "applies_to": "TASK",
                "is_default": True,
                "is_active": True,
                "created_at": SEED_EPOCH.isoformat(),
            },
        },
    ]

    state_ids: dict[str, dict[str, int]] = {"PROJECT": {}, "TASK": {}}
    definitions = (
        ("PROJECT", 1, PROJECT_STATES, 1),
        ("TASK", 2, TASK_STATES, 11),
    )
    for kind, workflow_pk, states, first_pk in definitions:
        for offset, (code, label, category, initial, terminal, order, color) in enumerate(states):
            pk = first_pk + offset
            state_ids[kind][code] = pk
            objects.append(
                {
                    "model": "workflow.workflowstate",
                    "pk": pk,
                    "fields": {
                        "workflow": workflow_pk,
                        "code": code,
                        "label": label,
                        "category": category,
                        "is_initial": initial,
                        "is_terminal": terminal,
                        "order": order,
                        "color": color,
                    },
                }
            )

    transition_pk = 0
    for kind, workflow_pk, transitions in (
        ("PROJECT", 1, PROJECT_TRANSITIONS),
        ("TASK", 2, TASK_TRANSITIONS),
    ):
        for from_code, to_code, label, requires_reason, requires_fields, order in transitions:
            transition_pk += 1
            objects.append(
                {
                    "model": "workflow.workflowtransition",
                    "pk": transition_pk,
                    "fields": {
                        "workflow": workflow_pk,
                        "from_state": state_ids[kind][from_code],
                        "to_state": state_ids[kind][to_code],
                        "label": label,
                        "requires_reason": requires_reason,
                        "requires_fields": requires_fields,
                        "guard": "",
                        "is_active": True,
                        "order": order,
                    },
                }
            )

    objects.extend(
        [
            {
                "model": "workflow.workflowbinding",
                "pk": 1,
                "fields": {
                    "workflow": 1,
                    "applies_to": "PROJECT",
                    "engagement_type": None,
                    "is_active": True,
                },
            },
            {
                "model": "workflow.workflowbinding",
                "pk": 2,
                "fields": {
                    "workflow": 2,
                    "applies_to": "TASK",
                    "engagement_type": None,
                    "is_active": True,
                },
            },
        ]
    )
    return objects, state_ids


def build_portfolio(
    dataset: dict[str, list[dict[str, Any]]],
    catalog_ids: dict[str, dict[str, int]],
    user_ids: dict[str, int],
    project_state_ids: dict[str, int],
) -> tuple[list[FixtureObject], dict[str, int]]:
    """Build the clients and the 22 projects.

    ``workflow_state`` comes from ``stage`` except for the rows in
    :data:`AUTHORED_PROJECT_STATES`; ``imported_health`` keeps the source ``health`` verbatim as a
    cross-check and is read by nothing at runtime — the derived risk flags win (DATA_MODEL §3).

    ``next_step`` is left empty everywhere because the source has no such column. That is a finding,
    not a gap to paper over: ``HasNoNextStep`` is supposed to fire on a project nobody has said
    what to do with next.

    Args:
        dataset: The parsed source workbook.
        catalog_ids: Taxonomy label -> pk, per model.
        user_ids: Person alias -> pk.
        project_state_ids: Project ``WorkflowState.code`` -> pk.

    Returns:
        The fixture objects and ``project_code -> pk``.
    """
    aliases = sorted({project["client_alias"] for project in dataset["Projects"]})
    client_ids = {alias: pk for pk, alias in enumerate(aliases, start=1)}

    objects: list[FixtureObject] = [
        {
            "model": "portfolio.client",
            "pk": client_ids[alias],
            "fields": {
                "code": _slug(alias),
                "alias": alias,
                "notes": "",
                "is_active": True,
                "created_at": SEED_EPOCH.isoformat(),
            },
        }
        for alias in aliases
    ]

    project_ids: dict[str, int] = {}
    for pk, row in enumerate(dataset["Projects"], start=1):
        code = str(row["project_code"])
        project_ids[code] = pk
        authored = AUTHORED_PROJECT_STATES.get(code)
        state_code = authored[0] if authored else STAGE_TO_PROJECT_STATE[row["stage"]]
        objects.append(
            {
                "model": "portfolio.project",
                "pk": pk,
                "fields": {
                    "code": code,
                    "name": row["project_name"],
                    "client": client_ids[row["client_alias"]],
                    "engagement_type": catalog_ids["engagementtype"][row["engagement_type"]],
                    "project_type": catalog_ids["projecttype"][row["project_type_api"]],
                    "stage": catalog_ids["stage"][row["stage"]],
                    "workflow_state": project_state_ids[state_code],
                    "owner": user_ids[row["owner_alias"]],
                    "start_date": _iso_date(row["start_date"]),
                    "target_date": _iso_date(row["target_date"]),
                    "business_value": _decimal_or_none(row["business_value"]),
                    "currency": row["currency"],
                    "summary": row["summary"] or "",
                    "next_step": "",
                    "is_archived": False,
                    "imported_health": row["health"],
                    # Dated from its own start where the source has one: a project that began in
                    # November did not appear in the system in January.
                    "created_at": _started_at(row["start_date"]).isoformat(),
                    "updated_at": ACTIVITY_ANCHOR.isoformat(),
                },
            }
        )
    return objects, project_ids


def build_work(
    dataset: dict[str, list[dict[str, Any]]],
    catalog_ids: dict[str, dict[str, int]],
    user_ids: dict[str, int],
    project_ids: dict[str, int],
    task_state_ids: dict[str, int],
) -> tuple[list[FixtureObject], dict[str, int]]:
    """Build tasks, dependency edges, blockers and notes.

    Every ``Blocker`` and ``Note`` carries an explicit ``code`` because ``loaddata`` never runs
    ``Model.save()``, where those codes are drawn. The counts returned here are what
    ``sync_code_sequences`` must advance the two PostgreSQL sequences past; getting that wrong
    makes the *first* runtime insert collide on the unique constraint, long after the seed looked
    successful.

    Args:
        dataset: The parsed source workbook.
        catalog_ids: Taxonomy label -> pk, per model.
        user_ids: Person alias -> pk.
        project_ids: Project code -> pk.
        task_state_ids: Task ``WorkflowState.code`` -> pk.

    Returns:
        The fixture objects and ``{"blockers": n, "notes": n}`` — the sequence high-water marks.
    """
    tasks = dataset["Tasks"]
    titles = _dependency_index(tasks)
    task_created_at = (ACTIVITY_ANCHOR - timedelta(days=TASK_AGE_DAYS)).isoformat()
    task_ids: dict[str, int] = {row["task_code"]: pk for pk, row in enumerate(tasks, start=1)}

    objects: list[FixtureObject] = []
    for row in tasks:
        objects.append(
            {
                "model": "work.task",
                "pk": task_ids[row["task_code"]],
                "fields": {
                    "code": row["task_code"],
                    "project": project_ids[row["project_code"]],
                    "assignee": user_ids[row["assignee_alias"]],
                    "priority": catalog_ids["priority"][row["priority"]],
                    "workflow_state": task_state_ids[TASK_STATUS_TO_STATE[row["status"]]],
                    # The source ``is_overdue`` string is not imported: overdue is derived from
                    # this date against now, so a seed loaded next month is still correct.
                    "due_date": row["due_date"],
                    "title": row["title"],
                    "detail": row["detail"] or "",
                    "last_progress": row["last_progress"] or "",
                    "created_at": task_created_at,
                    "updated_at": ACTIVITY_ANCHOR.isoformat(),
                },
            }
        )

    dependency_pk = 0
    for row in tasks:
        raw = row["dependency"]
        if not raw:
            continue
        label = str(raw).strip()
        target = titles[row["project_code"]].get(label)
        if target == row["task_code"]:
            # A task cannot depend on itself; the check constraint would reject the row.
            continue
        dependency_pk += 1
        objects.append(
            {
                "model": "work.taskdependency",
                "pk": dependency_pk,
                "fields": {
                    "task": task_ids[row["task_code"]],
                    "depends_on": task_ids[target] if target else None,
                    # Never dropped: an unresolvable dependency is still the operation's own note.
                    "raw_label": "" if target else label,
                    "is_resolved": target is not None,
                    "created_at": task_created_at,
                },
            }
        )

    blocker_pk = 0
    blocked_projects = 0
    for row in dataset["Projects"]:
        paragraph = row["blockers"]
        if not paragraph:
            continue
        age = BLOCKER_AGE_DAYS_CYCLE[blocked_projects % len(BLOCKER_AGE_DAYS_CYCLE)]
        blocked_projects += 1
        raised_at = (ACTIVITY_ANCHOR - timedelta(days=age)).isoformat()
        for sentence in _sentences(str(paragraph)):
            for kind in _blocker_kinds(sentence):
                blocker_pk += 1
                objects.append(
                    {
                        "model": "work.blocker",
                        "pk": blocker_pk,
                        "fields": {
                            "code": f"BLK-{blocker_pk:04d}",
                            "project": project_ids[row["project_code"]],
                            "task": None,
                            "description": sentence,
                            "kind": kind,
                            "raised_at": raised_at,
                            "resolved_at": None,
                            "owner": user_ids[row["owner_alias"]],
                            "resolution_reason": "",
                        },
                    }
                )

    note_pk = 0
    for row in dataset["Projects"]:
        examples = row["recent_completed_examples"]
        if not examples or examples == NO_COMPLETED_EXAMPLES:
            continue
        note_pk += 1
        objects.append(
            {
                "model": "work.note",
                "pk": note_pk,
                "fields": {
                    "code": f"NOTE-{note_pk:04d}",
                    "project": project_ids[row["project_code"]],
                    "task": None,
                    "body": f"Recently completed: {examples}",
                    "author": "system",
                    "created_at": ACTIVITY_ANCHOR.isoformat(),
                },
            }
        )

    return objects, {"blockers": blocker_pk, "notes": note_pk}


def build_activity(project_ids: dict[str, int]) -> list[FixtureObject]:
    """Build one ``SEEDED`` audit record per project.

    Two reasons, and only the first is about the trail. A seeded aggregate that appears with no
    recorded origin contradicts CLAUDE.md rule 3. And ``last_activity_at`` is read from this table
    by the ``staleness`` signal and by ``IsStale``: with the table empty every project is maximally
    stale and the signal degenerates to a constant.

    ``occurred_at`` is **authored** — see :data:`ACTIVITY_ANCHOR`. The spread is deterministic, so
    two runs of the generator produce the same file and two ``loaddata`` runs the same rows.

    Args:
        project_ids: Project code -> pk, iterated in code order.

    Returns:
        One record per project.
    """
    return [
        {
            "model": "activity.activityrecord",
            "pk": index,
            "fields": {
                "entity_type": "project",
                "entity_id": code,
                "verb": "SEEDED",
                "origin": "SYSTEM",
                "actor": "system",
                "from_value": "",
                "to_value": "",
                "reason": "Imported from the operational spreadsheet.",
                "metadata": {"source": "dataset.json"},
                "occurred_at": (
                    ACTIVITY_ANCHOR - timedelta(days=(index - 1) % ACTIVITY_SPREAD_DAYS)
                ).isoformat(),
                "correlation_id": str(SEED_CORRELATION_ID),
            },
        }
        for index, code in enumerate(project_ids, start=1)
    ]


def build_prioritization() -> list[FixtureObject]:
    """Build the single active ``PriorityPolicy``.

    Scores and risk flags are **not** fixtures: they are derived, and shipping them would let a
    reviewer read a number that no longer follows from the rows next to it. ``manage.py recompute``
    produces them after the load.

    Returns:
        The one policy row.
    """
    return [
        {
            "model": "prioritization.prioritypolicy",
            "pk": 1,
            "fields": {
                "version": POLICY_VERSION,
                "is_active": True,
                "weights": POLICY_WEIGHTS,
                "modifiers": POLICY_MODIFIERS,
                "notes": "Seed policy: the weights of ARCHITECTURE §4.1.",
                "created_at": SEED_EPOCH.isoformat(),
            },
        }
    ]


def main() -> None:
    """Generate every fixture from the dataset and report what was written.

    Raises:
        FileNotFoundError: ``data/raw/dataset.json`` is missing, which means the normalization
            step has not run — generating from nothing would silently emit empty fixtures and
            wipe the seed on the next load.
    """
    if not DATASET_PATH.exists():
        message = f"Dataset not found at {DATASET_PATH}"
        raise FileNotFoundError(message)

    dataset: dict[str, list[dict[str, Any]]] = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

    catalog, catalog_ids = build_catalog()
    accounts, user_ids = build_accounts(dataset, catalog_ids["role"])
    workflow, state_ids = build_workflow(catalog_ids["engagementtype"])
    portfolio, project_ids = build_portfolio(dataset, catalog_ids, user_ids, state_ids["PROJECT"])
    work, sequence_marks = build_work(
        dataset, catalog_ids, user_ids, project_ids, state_ids["TASK"]
    )
    activity = build_activity(project_ids)
    prioritization = build_prioritization()

    for app_label, objects in (
        ("catalog", catalog),
        ("accounts", accounts),
        ("workflow", workflow),
        ("portfolio", portfolio),
        ("work", work),
        ("activity", activity),
        ("prioritization", prioritization),
    ):
        path = _write(app_label, objects)
        print(f"{path.relative_to(REPO_ROOT)}: {len(objects)} objects")

    print(
        "sequence high-water marks -> "
        f"work_blocker_code_seq={sequence_marks['blockers']}, "
        f"work_note_code_seq={sequence_marks['notes']}"
    )


if __name__ == "__main__":
    main()

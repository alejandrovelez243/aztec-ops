"""``manage.py seed`` — take an empty database to a fully scored board in one command.

**The only management command in this project, and that is a rule rather than an accident.** A
command someone runs from a laptop against the production database is not an operation, it is an
accident waiting for a shell prompt: no audit trail, no permission check, no undo. Everything else
that used to be a command is now reachable where the operator already is — ``recompute`` is an
admin action on ``Project`` and an API endpoint, ``sync_code_sequences`` is step 2 below,
``run_relay`` and ``run_consumer`` died with the Redis Streams machinery. ``seed`` survives because
it is bootstrap: it runs inside the container at deploy time, against a database that has nothing
in it yet.

Three steps that must happen in this order and that nobody should have to remember:

1. ``loaddata`` over the seven fixtures **in FK order**, never a glob. A glob orders by filename,
   and ``accounts`` before ``catalog`` fails on a role that does not exist yet.
2. ``sync_code_sequences()``, because ``loaddata`` bypasses ``Model.save()`` and therefore leaves
   ``work_blocker_code_seq`` and ``work_note_code_seq`` at 1 while the tables already hold
   ``BLK-0053`` — a collision that would surface on the first blocker a user raises, not here. It
   is called unconditionally and has no flag to skip it: a repair that can be skipped is a repair
   that gets skipped.
3. ``recompute_active_portfolio()``, because ``PriorityScore`` is derived and is deliberately not
   a fixture: a committed score would be a number a reviewer could read that no longer follows from
   the rows beside it. Risk flags need no such step at all — they are computed on read (ADR 0011),
   so a freshly seeded portfolio is correctly flagged before anything has run.

The whole thing is an upsert. Every fixture object carries an explicit ``pk``, so a second run
updates the same rows, the sequence sync re-reads the same high-water mark, and every
recomputation reports ``changed=False`` because the input hash and the policy version still match.
Running ``seed`` twice must leave the row counts identical; if it ever does not, one of those three
properties has been broken.
"""

from typing import Any, Final

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandParser

from apps.prioritization.services import recompute_active_portfolio
from apps.work.services.sync_code_sequences import sync_code_sequences

#: Fixture labels in foreign-key dependency order (DATA_MODEL §14). ``loaddata`` resolves each
#: label against every app's ``fixtures/`` directory, so the names are unique on purpose.
FIXTURES: Final[tuple[str, ...]] = (
    "catalog",
    "accounts",
    "workflow",
    "portfolio",
    "work",
    "activity",
    "prioritization",
)


class Command(BaseCommand):
    """Load the committed fixtures, realign the code sequences and rebuild the ranking.

    Assumes migrations have already been applied; it does not run them, because a command that
    silently migrates is a command that silently migrates production. Fails loudly on a database
    whose schema is behind rather than half-loading.
    """

    help = "Load the seed fixtures, sync the business-code sequences and recompute every score."

    def add_arguments(self, parser: CommandParser) -> None:
        """Allow skipping the ranking rebuild when only the rows are wanted."""
        parser.add_argument(
            "--no-recompute",
            action="store_true",
            help="Load and sync only; leave PriorityScore untouched.",
        )

    def handle(self, *_args: Any, **options: Any) -> None:
        """Run the steps in order and report each one."""
        call_command("loaddata", *FIXTURES, verbosity=0)
        self.stdout.write(f"Loaded fixtures: {', '.join(FIXTURES)}")

        for sync in sync_code_sequences():
            self.stdout.write(
                self.style.SUCCESS(f"{sync.sequence} -> {sync.highest} (next {sync.next_code})")
            )

        self._apply_credentials()

        if options["no_recompute"]:
            self.stdout.write(self.style.WARNING("Skipped recompute; scores may be stale."))
            return

        run = recompute_active_portfolio()
        for result in run.results:
            flags = ",".join(result.flags) or "-"
            self.stdout.write(
                f"{result.project_code}: {result.value} [{result.health}] flags={flags}"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Recomputed {len(run.results)} project(s); "
                f"{run.changed_count} changed. Seed complete."
            )
        )

    def _apply_credentials(self) -> None:
        """Give the seeded accounts a password, but only from the environment.

        The committed fixture stores the literal ``"!"``, which Django reads as an unusable
        password: deterministic, so regenerating the fixtures produces no diff churn, and
        unusable, so a public repository ships no working credential.

        Nothing here has a default. A fallback that works is still a hardcoded credential,
        only one that everybody who reads the repository knows. Leaving the accounts
        unusable until an operator sets ``SEED_USER_PASSWORD`` is the safe failure; shipping
        ``admin/admin`` is not.
        """
        user_model = get_user_model()

        if settings.SEED_USER_PASSWORD:
            seeded = user_model.objects.filter(is_superuser=False)
            for user in seeded:
                user.set_password(settings.SEED_USER_PASSWORD)
            user_model.objects.bulk_update(seeded, ["password"])
            self.stdout.write(
                self.style.SUCCESS(f"Set SEED_USER_PASSWORD on {len(seeded)} account(s).")
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "SEED_USER_PASSWORD unset: seeded accounts keep an unusable password "
                    "and cannot sign in. Set it in .env to log in as a team member."
                )
            )

        if not (settings.SUPERUSER_USERNAME and settings.SUPERUSER_PASSWORD):
            self.stdout.write(
                self.style.WARNING(
                    "DJANGO_SUPERUSER_USERNAME/_PASSWORD unset: no admin account created. "
                    "Set both in .env, or run `manage.py createsuperuser` yourself."
                )
            )
            return

        admin, created = user_model.objects.get_or_create(
            username=settings.SUPERUSER_USERNAME,
            defaults={
                "code": settings.SUPERUSER_USERNAME,
                "alias": settings.SUPERUSER_USERNAME,
                "email": settings.SUPERUSER_EMAIL,
                "is_staff": True,
                "is_superuser": True,
            },
        )
        admin.set_password(settings.SUPERUSER_PASSWORD)
        admin.is_staff = True
        admin.is_superuser = True
        admin.save(update_fields=["password", "is_staff", "is_superuser"])
        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} superuser {settings.SUPERUSER_USERNAME}."))

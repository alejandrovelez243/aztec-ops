"""``manage.py run_relay`` — the compose service that publishes the outbox."""

import logging
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.events.relay import DEFAULT_BATCH_SIZE, DEFAULT_POLL_INTERVAL_SECONDS, OutboxRelay

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Run the outbox relay in the foreground.

    A thin wrapper: everything that can fail lives in ``apps.events.relay``, so the behaviour
    under Compose and the behaviour under ``make relay`` are the same code. Safe to run in more
    than one process — the claim uses ``SKIP LOCKED``.
    """

    help = "Publish committed OutboxEvent rows to the Redis stream, continuously."

    def add_arguments(self, parser: CommandParser) -> None:
        """Expose the two knobs worth turning while debugging a delivery problem."""
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help="Rows claimed per pass.",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=DEFAULT_POLL_INTERVAL_SECONDS,
            help="Seconds to wait when the backlog is empty.",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Drain a single batch and exit, for scripts and tests.",
        )

    def handle(self, *_args: Any, **options: Any) -> None:
        """Drain once or loop forever, and report the count on the single-pass path."""
        relay = OutboxRelay(batch_size=options["batch_size"])
        if options["once"]:
            published = relay.drain_once()
            self.stdout.write(self.style.SUCCESS(f"Published {len(published)} event(s)."))
            return
        try:
            relay.run_forever(poll_interval_seconds=options["poll_interval"])
        except KeyboardInterrupt:
            self.stdout.write("Relay stopped.")

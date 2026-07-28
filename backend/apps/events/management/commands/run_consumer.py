"""``manage.py run_consumer <group>`` — run one registered consumer group in the foreground."""

import logging
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.events.consumers.base import ConsumerRunner, get_consumer, registered_groups
from apps.events.domain.errors import ConsumerGroupNotRegisteredError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Read ``aztec.events`` as one consumer group until the process is stopped.

    A thin wrapper over ``apps.events.consumers.base``. One process per group, so a group that
    fails or falls behind does not affect the others — that separation is the reason there are
    groups at all.
    """

    help = "Run a registered consumer group against the aztec.events stream."

    def add_arguments(self, parser: CommandParser) -> None:
        """Take the group name, plus the consumer name when several replicas share a group."""
        parser.add_argument(
            "group",
            type=str,
            help=f"Consumer group. Registered: {', '.join(registered_groups()) or '(none)'}.",
        )
        parser.add_argument(
            "--consumer-name",
            type=str,
            default=None,
            help="Unique name within the group. Defaults to <group>@<hostname>.",
        )

    def handle(self, *_args: Any, **options: Any) -> None:
        """Resolve the group to its registered class and run it.

        Raises:
            CommandError: The group is not registered, which is a deployment or wiring mistake
                rather than a runtime failure, so it must stop the process loudly instead of
                looping over an empty stream.
        """
        group: str = options["group"]
        try:
            consumer = get_consumer(group)
        except ConsumerGroupNotRegisteredError as error:
            raise CommandError(str(error)) from error

        runner = ConsumerRunner(consumer, consumer_name=options["consumer_name"])
        self.stdout.write(self.style.SUCCESS(f"Consumer {group} listening."))
        try:
            runner.run_forever()
        except KeyboardInterrupt:
            self.stdout.write(f"Consumer {group} stopped.")

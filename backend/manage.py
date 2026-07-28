#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys


def main() -> None:
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # Imported after DJANGO_SETTINGS_MODULE is set: importing Django first would
    # evaluate settings against the wrong module. This is Django's own pattern.
    from django.core.management import execute_from_command_line  # noqa: PLC0415

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()

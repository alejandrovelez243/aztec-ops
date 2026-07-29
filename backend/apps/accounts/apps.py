"""App configuration for the identity context."""

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Holds the swappable ``AUTH_USER_MODEL``; loaded before any context that references it."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"

"""Django app configuration for the catalog bounded context."""

from django.apps import AppConfig


class CatalogConfig(AppConfig):
    """The configurable taxonomies the operation edits without a deploy."""

    name = "apps.catalog"

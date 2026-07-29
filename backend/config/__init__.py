"""Django project configuration: settings, URLs, Celery and the ASGI/WSGI entry points."""

from config.celery import app as celery_app

__all__ = ("celery_app",)

"""ASGI entry point.

The API runs under ASGI because SSE needs it: a WSGI worker holds a thread per
open stream, so a handful of dashboards left open would exhaust the pool.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.asgi import get_asgi_application

application = get_asgi_application()

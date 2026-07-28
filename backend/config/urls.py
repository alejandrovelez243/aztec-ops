"""URL routing.

Three surfaces, and the split between them is deliberate.

``/api/v1/`` is the versioned REST contract, mounted from :mod:`config.api`; its OpenAPI document
lands at ``/api/v1/openapi.json``, which is what the frontend's types are generated from.

``/api/stream`` is **unversioned on purpose** (`docs/API.md` §1.1): a browser tab holds it open
across deploys and the envelope carries its own ``version`` field, so versioning the path would
break every open dashboard on every release. It is a plain async Django view rather than a ninja
operation because its response is an open-ended byte stream, not a schema.

``/admin/`` is where the operation edits taxonomies, workflows and transitions without a deploy. It
is an operator tool, not an API, and nothing in the contract depends on it.
"""

from django.contrib import admin
from django.urls import path

from config.api import api
from config.sse import event_stream

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
    path("api/stream", event_stream, name="event-stream"),
]

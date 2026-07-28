"""URL routing.

The API is mounted under ``/api`` by :mod:`config.api`; the admin is where the
operation edits taxonomies, workflows and transitions without a deploy.
"""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
]

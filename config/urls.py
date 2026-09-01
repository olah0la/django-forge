"""Root URL configuration.

Kept deliberately thin. Applications do not add routes here directly: each app
contributes a django-ninja router, mounted by `config/api.py`, and this module
mounts the single API instance in one line. That keeps this file from becoming a
permanent merge-conflict site as the project grows.
"""

from django.contrib import admin
from django.urls import path

from config.api import V1_PREFIX, api

urlpatterns = [
    path("admin/", admin.site.urls),
    # The version prefix is part of the URL from the very first endpoint, and
    # this is the only line that knows where the API is mounted. Adding a
    # prefix later is a breaking change for every integrated client, so it is
    # cheaper to carry five characters now than to coordinate that migration
    # ever. See docs/api.md — a future v2 is a SECOND line here, added beside
    # this one rather than replacing it.
    path(f"api/{V1_PREFIX}/", api.urls),
]

"""Root URL configuration.

Kept deliberately thin. Applications do not add routes here directly: from
M5-02 each app contributes a django-ninja router, and this module mounts the
single API instance. That keeps this file from becoming a permanent
merge-conflict site as the project grows.
"""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
    # TODO(M5-01): mount the versioned API here, e.g.
    #     path("api/v1/", api.urls)
]

"""Root URL configuration.

Kept deliberately thin. Applications do not add routes here directly: each app
contributes a django-ninja router, mounted by `config/api.py`, and this module
mounts the single API instance in one line. That keeps this file from becoming a
permanent merge-conflict site as the project grows.

Three things are exempt from that rule, and all for the same reason — they are
project wiring rather than application endpoints: the admin, the health probes
below, and serving user uploads in development.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

from apps.core.health import liveness, readiness
from config.api import V1_PREFIX, api

# Unpacked so the paths are named in exactly one place (config/settings/base.py)
# and the URLconf, the SSL-redirect exemption and the access-log filter cannot
# drift apart. The tuple is fixed-length by contract; an accidental third entry
# should fail loudly here rather than 404 silently at 3am.
LIVENESS_PATH, READINESS_PATH = settings.HEALTH_CHECK_PATHS

urlpatterns = [
    path("admin/", admin.site.urls),
    # UNVERSIONED, on purpose, and not behind /api/v1/.
    #
    # A probe URL is an infrastructure contract — it lives in a Dockerfile, a
    # Compose file, a Kubernetes manifest, a load balancer's configuration. The
    # API prefix is a contract boundary for API CLIENTS, and a v2 that moved
    # these would break every deployment that ever adopted them, for a reason
    # unrelated to the API changing at all.
    #
    # Mounting them here also means they keep answering when the NinjaAPI
    # instance fails to build, which is precisely the moment a probe earns its
    # keep. See apps/core/health.py and docs/ops.md.
    #
    # No trailing slash, so APPEND_SLASH never enters the picture: the path a
    # probe requests is the path that resolves.
    path(LIVENESS_PATH.lstrip("/"), liveness, name="liveness"),
    path(READINESS_PATH.lstrip("/"), readiness, name="readiness"),
    # The version prefix is part of the URL from the very first endpoint, and
    # this is the only line that knows where the API is mounted. Adding a
    # prefix later is a breaking change for every integrated client, so it is
    # cheaper to carry five characters now than to coordinate that migration
    # ever. See docs/api.md — a future v2 is a SECOND line here, added beside
    # this one rather than replacing it.
    path(f"api/{V1_PREFIX}/", api.urls),
]

# Uploads, in development only (M6-03).
#
# `static()` returns an EMPTY LIST when DEBUG is false, so this line is its own
# guard and needs no `if` around it — and production never serves a user upload
# through Django, which is the point. Nothing here is a production route.
#
# It exists because an ImageField is unviewable without it: the file is written
# to MEDIA_ROOT and every URL pointing at it 404s, which reads as a broken
# upload rather than as a missing route.
#
# WhiteNoise deliberately does NOT do this job. It serves STATIC_ROOT — files
# that ship with the code and are safe to cache for a year. User uploads are
# neither, and the place they belong in production is object storage. See the
# MEDIA block in config/settings/base.py and docs/serving.md.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

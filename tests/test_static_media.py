"""The static and media file strategy (M6-03).

Two different problems share this file because Django's settings for them share
a shape, and conflating them is the mistake most of these tests guard.

**Static** is a build-time property. `collectstatic` runs in the image, the
production layer hashes and compresses what it collected, and WhiteNoise serves
the result. The silent failure mode is a settings edit that moves the
collection back to container start, or drops the manifest backend — nothing
raises at import, and the break appears as an unstyled page in production.

**Media** is a durability property, and its failure mode is worse than silent:
uploads written to a container's filesystem work in every environment a
developer can see and are destroyed on the first deploy. There is no assertion
that can catch that at run time, so what is guarded here is the thing that
prevents it — that the storage backend stays substitutable, and that the
documentation stating the warning has not quietly lost it.

Follows the habit established by test_settings.py (which parses
docker-compose.yml) and test_docs.py: non-Python files are fair game when they
carry an acceptance criterion.
"""

import json
import re
from pathlib import Path

import pytest
from django.conf import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"
SERVING_DOC = REPO_ROOT / "docs" / "serving.md"

PRODUCTION_ENV = {
    "DJANGO_SECRET_KEY": "x" * 50,
    "DJANGO_ALLOWED_HOSTS": "example.com",
}

FILESYSTEM_STORAGE = "django.core.files.storage.FileSystemStorage"
WHITENOISE_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
PLAIN_STATIC_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"


# ---------------------------------------------------------------------------
# URLs and roots are correct in every layer
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("layer", ["development", "production"])
def test_static_and_media_settings_are_present_and_distinct(load_settings, layer):
    """Both profiles need all four, and the two roots must not collide.

    A shared root is not a hypothetical slip: it makes `collectstatic --clear`
    delete user uploads, and it publishes every upload under STATIC_URL, where
    WhiteNoise will serve it to anyone who guesses the name.
    """
    rc, out, err = load_settings(
        layer,
        PRODUCTION_ENV,
        "{'static_url': settings.STATIC_URL, 'static_root': str(settings.STATIC_ROOT),"
        " 'media_url': settings.MEDIA_URL, 'media_root': str(settings.MEDIA_ROOT)}",
    )
    assert rc == 0, err
    values = json.loads(out)

    assert values["static_url"] == "/static/", values["static_url"]
    assert values["media_url"] == "/media/", values["media_url"]
    assert values["static_root"].endswith("/staticfiles"), values["static_root"]
    assert values["media_root"].endswith("/mediafiles"), values["media_root"]
    assert values["static_root"] != values["media_root"], (
        "STATIC_ROOT and MEDIA_ROOT must not be the same directory: collectstatic "
        "would then be able to delete user uploads"
    )


# ---------------------------------------------------------------------------
# The staticfiles backend is a per-layer decision
# ---------------------------------------------------------------------------
def test_production_uses_the_whitenoise_manifest_backend(load_settings):
    """Hashed filenames and pre-compression are what make the cache safe.

    Without the manifest backend, `Cache-Control: immutable` cannot be used at
    all: an unhashed name means a deploy ships new HTML against a browser's
    cached old stylesheet, with no way to invalidate it.
    """
    rc, out, err = load_settings(
        "production",
        PRODUCTION_ENV,
        "{'backend': settings.STORAGES['staticfiles']['BACKEND']}",
    )
    assert rc == 0, err
    assert json.loads(out)["backend"] == WHITENOISE_STORAGE


@pytest.mark.parametrize("layer", ["development", "test"])
def test_non_production_layers_do_not_use_the_manifest_backend(load_settings, layer):
    """The manifest backend requires collectstatic to have run first.

    `Manifest...` reads staticfiles.json and raises ValueError on a miss, so
    adopting it here would make every {% static %} call fail in a checkout that
    has not run a build — and make the test suite depend on one.
    """
    rc, out, err = load_settings(
        layer, PRODUCTION_ENV, "{'backend': settings.STORAGES['staticfiles']['BACKEND']}"
    )
    assert rc == 0, err
    assert json.loads(out)["backend"] == PLAIN_STATIC_STORAGE, (
        f"{layer} must not require a collected manifest"
    )


# ---------------------------------------------------------------------------
# Media storage stays substitutable — the acceptance criterion
# ---------------------------------------------------------------------------
def test_default_file_storage_falls_back_to_the_filesystem(load_settings):
    rc, out, err = load_settings(
        "production", PRODUCTION_ENV, "{'backend': settings.STORAGES['default']['BACKEND']}"
    )
    assert rc == 0, err
    assert json.loads(out)["backend"] == FILESYSTEM_STORAGE


def test_default_file_storage_is_swappable_by_environment(load_settings):
    """The whole media strategy rests on this being one variable.

    An adopter must be able to move uploads to object storage without editing
    the template's code, or they will not do it — and the container filesystem
    default is the one that loses data.
    """
    substitute = "storages.backends.s3.S3Storage"
    rc, out, err = load_settings(
        "production",
        {**PRODUCTION_ENV, "DJANGO_DEFAULT_FILE_STORAGE": substitute},
        "{'backend': settings.STORAGES['default']['BACKEND']}",
    )
    assert rc == 0, err
    assert json.loads(out)["backend"] == substitute, (
        "DJANGO_DEFAULT_FILE_STORAGE did not reach STORAGES['default']"
    )


def test_an_unimportable_storage_backend_fails_loudly(load_settings):
    """A silent fallback to local disk is the failure this must not have.

    Falling back would put uploads on an ephemeral filesystem because someone
    typed a dotted path wrong — the exact outcome the substitution point exists
    to prevent, arrived at by accident.

    Measured: settings import SUCCEEDS (the path is resolved lazily) and the
    first file access raises InvalidStorageError naming the module.
    """
    rc, out, err = load_settings(
        "production",
        {**PRODUCTION_ENV, "DJANGO_DEFAULT_FILE_STORAGE": "nonexistent.module.NoSuchStorage"},
        "{'probe': __import__('django.core.files.storage', fromlist=['x'])"
        ".default_storage.__class__.__name__}",
    )
    assert rc != 0, (
        f"a bad storage path must not resolve silently; it returned {out!r} instead of raising"
    )
    assert "InvalidStorageError" in err or "NoSuchStorage" in err, (
        f"the error must name the backend that could not be loaded: {err}"
    )


def test_swapping_media_storage_does_not_disturb_static(load_settings):
    """The two keys are independent, and a reader should not have to trust that."""
    rc, out, err = load_settings(
        "production",
        {**PRODUCTION_ENV, "DJANGO_DEFAULT_FILE_STORAGE": "storages.backends.s3.S3Storage"},
        "{'backend': settings.STORAGES['staticfiles']['BACKEND']}",
    )
    assert rc == 0, err
    assert json.loads(out)["backend"] == WHITENOISE_STORAGE


# ---------------------------------------------------------------------------
# Middleware ordering, which is wrong in both directions
# ---------------------------------------------------------------------------
def test_whitenoise_sits_directly_after_security_middleware():
    """Position is a correctness property, not a style preference.

    Above SecurityMiddleware, a static response skips the HTTPS redirect and
    the security headers — on the one kind of response a browser caches for a
    year. Below anything else, every request for an unchanging file opens a
    session and hits the database to return bytes that depend on neither.
    """
    mw = settings.MIDDLEWARE
    assert "whitenoise.middleware.WhiteNoiseMiddleware" in mw, "WhiteNoise is not installed"

    security = mw.index("django.middleware.security.SecurityMiddleware")
    whitenoise = mw.index("whitenoise.middleware.WhiteNoiseMiddleware")
    assert whitenoise == security + 1, (
        "WhiteNoiseMiddleware must come directly after SecurityMiddleware; "
        f"got {mw[security : whitenoise + 1]}"
    )


def test_static_is_served_without_a_collected_directory():
    """Development and the test suite must never need `collectstatic`.

    Both resolve through the staticfiles finders. If that regressed, this
    returns 404 and WhiteNoise additionally warns "No directory at:" — the
    exact combination that makes the admin look broken on a fresh checkout.
    """
    from django.test import Client

    response = Client().get("/static/admin/css/base.css")
    assert response.status_code == 200, (
        "static file not served; WHITENOISE_USE_FINDERS is what makes this work "
        "without a collected staticfiles/ directory"
    )


# ---------------------------------------------------------------------------
# Collection happens in the build, and nowhere else
# ---------------------------------------------------------------------------
def _collectstatic_run_step() -> str:
    """Return the RUN instruction that collects static, comments excluded.

    Matching on the first mention of `collectstatic` would match the comment
    block above it, which explains the command and would therefore satisfy any
    assertion about the command. Anchor on `RUN` and consume the continued
    lines instead.
    """
    dockerfile = DOCKERFILE.read_text()
    match = re.search(r"^RUN (?:[^\n]*\\\n)*[^\n]*collectstatic[^\n]*$", dockerfile, re.M)
    assert match, "no RUN instruction in the Dockerfile runs collectstatic"
    return match.group(0)


def test_collectstatic_runs_in_the_image_build():
    """At build time the image is self-contained; at startup it is not."""
    assert "collectstatic" in _collectstatic_run_step()


def test_collectstatic_runs_under_production_settings():
    """Development settings would collect an unhashed tree with no manifest.

    That failure is invisible at build time and appears as a ValueError from
    every {% static %} call once the image is actually served.
    """
    step = _collectstatic_run_step()
    assert "DJANGO_SETTINGS_MODULE=config.settings.production" in step, (
        f"collectstatic must run under the production settings layer; got: {step}"
    )


def test_collectstatic_does_not_run_at_container_start():
    """At startup it would repeat identical work on every replica of every deploy.

    It would also run inside the window the health check is timing. Same
    reasoning as migrations, which docker-entrypoint.sh already refuses to run.
    """
    assert "collectstatic" not in ENTRYPOINT.read_text(), (
        "collectstatic belongs in the image build, not the entrypoint"
    )


def test_the_build_step_hardcodes_no_secret_key():
    """`make audit` scans full git history; a literal key here would be a finding."""
    dockerfile = DOCKERFILE.read_text()
    for match in re.finditer(r"DJANGO_SECRET_KEY=(\S+)", dockerfile):
        value = match.group(1)
        assert value.startswith('"$('), (
            f"DJANGO_SECRET_KEY must be generated, not a literal; found {value!r}"
        )


# ---------------------------------------------------------------------------
# The ephemerality warning, and the absence that enforces it
# ---------------------------------------------------------------------------
def test_production_profile_declares_no_volumes():
    """A media volume would hide the problem exactly where it is cheapest to see.

    A volume is one host: it survives `docker compose down` on a laptop and
    loses the files anyway on the second replica or the first node
    replacement. app-prod runs the image as built, uploads included.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    app_prod = compose[compose.index("  app-prod:") :]
    assert not re.search(r"^    volumes:", app_prod, re.M), (
        "app-prod must not mount volumes — see docs/serving.md on why there is no media volume"
    )


def test_serving_doc_warns_about_ephemerality_before_teaching_uploads():
    """Position matters: the warning is worthless after the instructions.

    Mirrors test_docs.py's backup-disclaimer test — someone skimming for the
    command must hit the reason it is not enough first.
    """
    text = SERVING_DOC.read_text()
    warning = re.search(r"container filesystem is not storage", text, re.IGNORECASE)
    assert warning, "docs/serving.md no longer states that container storage is ephemeral"

    substitution = text.index("DJANGO_DEFAULT_FILE_STORAGE")
    assert warning.start() < substitution, (
        "the ephemerality warning must appear before the storage substitution instructions"
    )


@pytest.mark.parametrize(
    "pattern",
    [
        r"deploy",
        r"object storage",
        r"replac",
    ],
)
def test_serving_doc_names_why_uploads_are_lost(pattern):
    """Loose about wording, strict about the subject — as test_docs.py puts it."""
    text = SERVING_DOC.read_text()
    assert re.search(pattern, text, re.IGNORECASE), (
        f"docs/serving.md no longer explains {pattern!r} in the media section"
    )


@pytest.mark.parametrize("entry", ["/staticfiles/", "/mediafiles/"])
def test_generated_directories_are_git_ignored(entry):
    """Anchored, so an app directory named `media/` stays trackable."""
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    assert entry in gitignore, f"{entry} must stay git-ignored and anchored to the repository root"

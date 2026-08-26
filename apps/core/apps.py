from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Shared, cross-cutting code for the whole project.

    `name` must be the full dotted path. Apps live under `apps/`, so it is
    "apps.core" and not "core" — Django uses this string to find the app's
    models, migrations, and templates.
    """

    name = "apps.core"
    # Django's modern default. Set explicitly so a future change to the global
    # DEFAULT_AUTO_FIELD cannot silently alter this app's primary keys.
    default_auto_field = "django.db.models.BigAutoField"

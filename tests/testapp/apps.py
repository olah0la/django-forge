from django.apps import AppConfig


class TestAppConfig(AppConfig):
    """Concrete models for exercising the abstract bases. Test layer only."""

    name = "tests.testapp"

"""The ASGI entrypoint.

These guard the wiring, not Django itself: that `config.asgi:application`
exists, is an ASGI callable, and that the development-only static wrapper does
not leak into other layers.
"""

import inspect

from django.conf import settings


def test_asgi_application_is_importable():
    from config.asgi import application

    assert application is not None


def test_asgi_application_is_an_asgi_callable():
    """ASGI apps take (scope, receive, send)."""
    from config.asgi import application

    call = application.__call__
    params = list(inspect.signature(call).parameters)
    assert params[:3] == ["scope", "receive", "send"], params


def test_static_wrapper_is_not_applied_outside_debug():
    """The ASGIStaticFilesHandler wrapper is a development convenience.

    Serving static files from the application process is not a production
    strategy — M6-03 owns that — so it must not appear when DEBUG is off.
    The test layer runs with DEBUG=False, which is what makes this meaningful.
    """
    from config.asgi import application

    assert settings.DEBUG is False
    assert type(application).__name__ == "ASGIHandler"

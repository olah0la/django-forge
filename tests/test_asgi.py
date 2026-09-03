"""The ASGI entrypoint.

These guard the wiring, not Django itself: that `config.asgi:application`
exists, is an ASGI callable, and that nothing wraps it on the way out.
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


def test_no_static_handler_wraps_the_application():
    """`ASGIStaticFilesHandler` is gone, in every layer (M6-03).

    It used to wrap the application under DEBUG, because uvicorn does not serve
    static files the way `runserver` did. WhiteNoise middleware does that job
    now, in every settings layer, so the mechanism exercised locally is the one
    that runs in production.

    Reintroducing the wrapper would be a regression in two directions: a second
    static mechanism that only ever runs in development, and a handler with no
    caching, compression or content hashing sitting in front of one that has
    all three. See config/asgi.py and docs/serving.md.
    """
    from config.asgi import application

    assert settings.DEBUG is False
    assert type(application).__name__ == "ASGIHandler", (
        f"something wrapped the ASGI application: {type(application).__name__}"
    )

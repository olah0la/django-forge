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


def _unwrap(app):
    """Every layer of the ASGI application, outermost first.

    `application` is a stack of wrappers — the shutdown lifespan handler always,
    the static files handler under DEBUG — and each holds the next in `.app`.
    Asserting on the outermost type alone would break every time a layer is
    added, which says nothing about whether the property under test still holds.
    """
    layers = []
    while app is not None:
        layers.append(app)
        app = getattr(app, "app", None)
    return layers


def test_static_wrapper_is_not_applied_outside_debug():
    """The ASGIStaticFilesHandler wrapper is a development convenience.

    Serving static files from the application process is not a production
    strategy — M6-03 owns that — so it must not appear when DEBUG is off.
    The test layer runs with DEBUG=False, which is what makes this meaningful.

    Checked across the whole wrapper stack, not just the outermost layer: the
    handler leaking in one layer down would serve static files from the
    application process just as effectively.
    """
    from config.asgi import application

    assert settings.DEBUG is False
    names = [type(layer).__name__ for layer in _unwrap(application)]
    assert "ASGIStaticFilesHandler" not in names, names
    assert "ASGIHandler" in names, names


def test_the_shutdown_handler_wraps_the_application():
    """M6-05. Django's handler rejects the lifespan scope, so without this
    wrapper uvicorn disables lifespan and the process has no shutdown hook.

    Applied in every layer, including this one: a drain that exists only in
    production is a drain nobody exercises until a deploy goes wrong.
    """
    from config.asgi import application

    assert type(application).__name__ == "ShutdownLifespanMiddleware", (
        "the shutdown handler must be OUTERMOST, or the lifespan scope reaches "
        "Django's handler and raises before it gets there"
    )

"""Windows reports a vanished browser as an unhandled exception.

The proactor event loop raises ConnectionResetError — WinError 10054 —
inside asyncio's own callback when a client closes a connection abruptly.
The HTMX pollers produce one every time a page is closed, and it reaches
the root logger, so it lands in the log viewer and buries the lines that
matter. What it never does is indicate a failure.
"""

from __future__ import annotations

import asyncio

from agsync.server import _ignore_client_disconnects


class _Loop:
    def __init__(self):
        self.handled: list[dict] = []

    def default_exception_handler(self, context):
        self.handled.append(context)


def test_a_reset_connection_is_dropped():
    loop = _Loop()
    _ignore_client_disconnects(
        loop,
        {"message": "...", "exception": ConnectionResetError(10054, "forcibly closed")},
    )
    assert loop.handled == []


def test_every_other_failure_still_reaches_the_default_handler():
    """An event loop that swallows its own errors is how silence starts."""
    loop = _Loop()
    for exc in (ValueError("real"), RuntimeError("real"), OSError("real")):
        _ignore_client_disconnects(loop, {"message": "...", "exception": exc})
    assert len(loop.handled) == 3


def test_a_context_without_an_exception_is_passed_through():
    loop = _Loop()
    _ignore_client_disconnects(loop, {"message": "something without an exception"})
    assert len(loop.handled) == 1


def test_the_handler_is_installed_on_startup(monkeypatch, tmp_path):
    """Set inside the lifespan, because uvicorn owns the loop."""
    monkeypatch.setenv("AG_SYNC_DB_PATH", str(tmp_path / "probe.db"))
    from agsync.config import get_settings
    get_settings.cache_clear()

    from agsync.server import create_app, lifespan

    async def main():
        app = create_app()
        async with lifespan(app):
            return asyncio.get_running_loop().get_exception_handler()

    assert asyncio.run(main()) is _ignore_client_disconnects

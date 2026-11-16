"""Regression: the OS trust store must be injected in the lifespan, not when
the app is built.

truststore.inject_into_ssl() patches ssl.SSLContext globally and is client-side
only. If it runs before uvicorn builds the HTTPS *server* context, the server
answers nothing (the browser sees ERR_EMPTY_RESPONSE). So building the app must
NOT inject; entering the lifespan — after uvicorn has its server context — must.
"""

import asyncio
import sys
import types

import agsync.trust as trust


def _fake_truststore(calls):
    mod = types.ModuleType("truststore")
    mod.inject_into_ssl = lambda: calls.append(1)
    return mod


def test_injection_happens_in_lifespan_not_at_app_build(monkeypatch, tmp_path):
    monkeypatch.setenv("AG_SYNC_DB_PATH", str(tmp_path / "trust.db"))
    from agsync.config import get_settings
    get_settings.cache_clear()

    monkeypatch.setattr(trust, "_injected", False)
    calls: list[int] = []
    monkeypatch.setitem(sys.modules, "truststore", _fake_truststore(calls))

    from agsync.server import create_app, lifespan

    app = create_app()
    # Building the app must not touch ssl — uvicorn's server context is built
    # from the stdlib class right after this, and must stay the stdlib one.
    assert calls == []

    async def enter():
        async with lifespan(app):
            return list(calls)

    # Startup injects, exactly once, after the server context already exists.
    assert asyncio.run(enter()) == [1]

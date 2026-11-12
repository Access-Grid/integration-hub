"""A Unity sync that goes wrong says why in the log, and the loop keeps going."""

import logging
import time

from agsync.lib.pacs.avigilon.client import UnityError
from agsync.sync import engine as engine_mod

from .test_unity_spec_end_to_end import _desfire
from .test_unity_spec_end_to_end import mailed as _mailed
from .test_unity_spec_end_to_end import world as _world

mailed, world = _mailed, _world


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_unity_read_that_fails_is_logged_with_its_reason_and_traceback(world, monkeypatch, caplog):
    _, _, cycle = _desfire(world, False)

    def unreachable(*a, **kw):
        raise UnityError("GET /identities/a/tokens.json: ConnectTimeout: timed out")

    monkeypatch.setattr(engine_mod, "build_snapshot", unreachable)
    result = cycle(may_fail=True)
    assert result.error.startswith("snapshot:")
    [record] = [r for r in _errors(caplog) if "Sync cycle stopped while reading" in r.getMessage()]
    message = record.getMessage()
    assert "avigilon" in message and "template tpl" in message and "nothing was changed" in message
    assert "UnityError: GET /identities/a/tokens.json: ConnectTimeout: timed out" in message
    assert record.exc_info is not None


def test_an_adapter_that_cannot_be_built_names_the_host(world, monkeypatch, caplog):
    _, _, cycle = _desfire(world, False)

    def broken(vendor, params):
        raise ValueError("bad host")

    monkeypatch.setattr(engine_mod, "build_pacs_adapter", broken)
    assert cycle(may_fail=True).error == "pacs_init: bad host"
    [record] = _errors(caplog)
    assert "avigilon adapter could not be built (host unity, mode desfire): bad host" in record.getMessage()
    assert record.exc_info is not None


def test_an_accessgrid_client_that_cannot_be_built_is_logged(world, monkeypatch, caplog):
    _, _, cycle = _desfire(world, False)

    def broken(*a):
        raise ValueError("bad secret")

    monkeypatch.setattr(engine_mod, "build_ag_client", broken)
    assert cycle(may_fail=True).error == "ag_init: bad secret"
    [record] = _errors(caplog)
    assert "AccessGrid client could not be built (account acct): bad secret" in record.getMessage()


def test_a_crashing_cycle_is_logged_and_the_loop_keeps_running(world, monkeypatch, caplog):
    _desfire(world, False)

    def locked():
        raise RuntimeError("settings database is locked")

    monkeypatch.setattr(engine_mod.AccessGridConfig, "load", locked)
    monkeypatch.setattr(engine_mod, "ERROR_BACKOFF_S", 0.05)
    engine = engine_mod.SyncEngine()
    engine.start()
    try:
        deadline = time.monotonic() + 3
        while engine.get_status().get("consecutive_errors", 0) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert engine._thread.is_alive(), "the sync thread outlives a crashing cycle"
        assert engine.get_status().get("consecutive_errors", 0) >= 2, "each crash counts toward the pause"
    finally:
        engine.stop()
    crashes = [r for r in caplog.records if r.getMessage().startswith("Sync cycle crashed")]
    assert crashes and crashes[0].exc_info is not None
    assert "RuntimeError: settings database is locked" in crashes[0].getMessage()
    assert any("Sync cycle failed (1 in a row; the engine pauses at 10): crashed: RuntimeError"
               in r.getMessage() for r in caplog.records)

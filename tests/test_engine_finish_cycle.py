"""The engine's end-of-cycle hook, for adapters that batch their PACS writes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs import PacsAuthExpired
from agsync.sync import engine as engine_mod
from agsync.sync.engine import SyncEngine
from agsync.sync.snapshot import Snapshot


@pytest.fixture
def cycle(monkeypatch):
    """One engine cycle with every collaborator faked; returns the call order."""
    calls: list[str] = []
    monkeypatch.setattr(engine_mod.AccessGridConfig, "load",
                        lambda: {"account_id": "a", "api_secret": "s", "template_id": "t"})
    monkeypatch.setattr(engine_mod.PacsConfig, "load",
                        lambda: {"vendor": "millennium_ultra", "params": {}, "options": {}})
    monkeypatch.setattr(engine_mod, "build_ag_client", lambda *a: object())
    monkeypatch.setattr(engine_mod, "build_snapshot",
                        lambda *a: Snapshot(people={"p": SimpleNamespace()}))
    for name in ("phase1_provision", "phase2_local_to_ag", "phase3_deletions",
                 "phase4_ag_to_local", "phase5_retries", "phase6_field_changes"):
        monkeypatch.setattr(getattr(engine_mod, name), "run",
                            lambda *a, _n=name, **k: calls.append(_n) or 0)
    monkeypatch.setattr("agsync.notifications.notify_reconnect_required",
                        lambda *a, **k: False)

    def run(finish):
        engine = SyncEngine()
        monkeypatch.setattr(engine, "_with_derived_mode", lambda ag, a, p: p)
        pacs = SimpleNamespace(supports_credential_writeback=False, finish_cycle=finish)
        monkeypatch.setattr(engine, "_pacs_adapter", lambda cfg: pacs)
        return engine, engine._run_one_cycle_locked()

    return calls, run


def test_finish_cycle_runs_after_every_phase(cycle):
    calls, run = cycle
    run(lambda: calls.append("finish_cycle"))
    assert calls[-2:] == ["phase6_field_changes", "finish_cycle"]


def test_an_expired_session_while_finishing_asks_for_a_reconnect(cycle):
    _, run = cycle

    def finish():
        raise PacsAuthExpired("session expired")

    engine, _ = run(finish)
    assert engine._status.reconnect_required is True

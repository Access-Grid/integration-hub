"""What the engine does around an expired PACS session.

Two failures met here in production: the reconnect banner survived a
successful reconnect until the next cycle *finished* — minutes, on a large
install — and repeated auth failures tripped the circuit breaker, pausing
the engine for a reason no amount of retrying could fix.
"""

from __future__ import annotations

import pytest

from agsync.sync.engine import MIN_INTERVAL_S, CycleResult, SyncEngine


@pytest.fixture
def engine() -> SyncEngine:
    return SyncEngine()


def _expired(engine: SyncEngine) -> CycleResult:
    result = CycleResult(started_at="now", duration_ms=0)
    return engine._handle_auth_expired("millennium_ultra", RuntimeError("gone"), result, 0.0)


# --- the flag ------------------------------------------------------------


def test_an_expired_session_raises_the_flag(engine):
    _expired(engine)
    status = engine.get_status()
    assert status["reconnect_required"] is True
    assert status["pacs_reachable"] is False


def test_capturing_a_session_lowers_it_at_once(engine):
    # The session is proven to read before it is stored, so waiting for a
    # cycle to finish would only mean staring at a stale demand.
    _expired(engine)
    engine.session_restored()
    status = engine.get_status()
    assert status["reconnect_required"] is False
    assert status["pacs_reachable"] is True


def test_capturing_a_session_also_unpauses_and_rebuilds(engine):
    engine._pacs = ("fingerprint", object())
    _expired(engine)
    engine.session_restored()
    assert engine.get_status()["paused"] is False
    assert engine.get_status()["consecutive_errors"] == 0
    # The cached adapter still holds the dead cookie.
    assert engine._pacs is None


# --- the circuit breaker -------------------------------------------------


def test_an_expired_session_does_not_count_toward_the_breaker(engine):
    # Ten of these used to pause the engine, so reconnecting was not enough
    # to get syncing again — it needed a restart too.
    assert _expired(engine).counts_as_failure is False


def test_an_abandoned_cycle_does_not_count_either(engine):
    result = engine._abandon(CycleResult(started_at="now", duration_ms=0), 0.0)
    assert result.error == "superseded"
    assert result.counts_as_failure is False


def test_an_ordinary_failure_still_counts():
    # The breaker must still exist for the failures it was built for.
    assert CycleResult(started_at="now", duration_ms=0, error="boom").counts_as_failure


# --- saying what it is doing --------------------------------------------


def test_the_next_run_time_is_in_the_future(engine, monkeypatch):
    """"Next sync" was set to the moment the cycle ended, not the next one.

    So the status page reported a next run a second or two after the last
    one, every time, while the engine actually slept for minutes.
    """
    from datetime import UTC, datetime

    from agsync.sync.engine import CycleResult

    monkeypatch.setattr(
        engine, "_run_one_cycle",
        lambda: CycleResult(started_at="now", duration_ms=20_000),
    )
    monkeypatch.setattr(engine._trigger, "wait", lambda timeout: engine._stop.set())
    engine._run_loop()

    status = engine.get_status()
    next_run = datetime.fromisoformat(status["next_run_iso"])
    # A 20s cycle wants 60s; the five-minute floor wins. Either way the
    # point of the assertion is that it is scheduled ahead, not at "now".
    assert (next_run - datetime.now(UTC)).total_seconds() > 30
    assert status["cached_interval_s"] == MIN_INTERVAL_S


def test_waiting_on_a_human_backs_off(engine, monkeypatch):
    # Each retry is a full roster sweep, and nothing changes until someone
    # signs in, so a ten-second retry is pure load on the PACS.
    from agsync.sync.engine import RECONNECT_RETRY_S, CycleResult

    def expired_cycle():
        return engine._handle_auth_expired(
            "millennium_ultra", RuntimeError("gone"),
            CycleResult(started_at="now", duration_ms=100), 0.0,
        )

    monkeypatch.setattr(engine, "_run_one_cycle", expired_cycle)
    monkeypatch.setattr(engine._trigger, "wait", lambda timeout: engine._stop.set())
    engine._run_loop()

    assert engine.get_status()["cached_interval_s"] == RECONNECT_RETRY_S


def test_the_loop_announces_each_cycle_and_the_wait(engine, monkeypatch, caplog):
    from agsync.sync.engine import CycleResult

    monkeypatch.setattr(
        engine, "_run_one_cycle",
        lambda: CycleResult(started_at="now", duration_ms=5_000),
    )
    monkeypatch.setattr(engine._trigger, "wait", lambda timeout: engine._stop.set())
    with caplog.at_level("INFO"):
        engine._run_loop()

    messages = [r.getMessage() for r in caplog.records]
    assert any("Sync cycle starting" in m for m in messages)
    assert any("Next sync cycle in" in m for m in messages)

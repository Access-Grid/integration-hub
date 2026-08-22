"""What the engine does around an expired PACS session.

Two failures met here in production: the reconnect banner survived a
successful reconnect until the next cycle *finished* — minutes, on a large
install — and repeated auth failures tripped the circuit breaker, pausing
the engine for a reason no amount of retrying could fix.
"""

from __future__ import annotations

import pytest

from agsync.sync.engine import CycleResult, SyncEngine


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

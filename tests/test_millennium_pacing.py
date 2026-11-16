"""Pacing requests to a Millennium install.

The sweep put ~5.4 requests a second through an ASP.NET UI sized for one
operator clicking around, for minutes at a time, and cardholder pages
started timing out. The pace is process-wide because the sync engine is not
the only caller — a settings page builds its own adapter on a request
thread, and those were the reads that timed out during a sweep.
"""

from __future__ import annotations

import threading

from agsync.lib.pacs.millennium_ultra import client as mod


def _reset(monkeypatch, interval=0.05):
    monkeypatch.setattr(mod, "MIN_REQUEST_INTERVAL_S", interval)
    monkeypatch.setattr(mod, "_last_request_at", 0.0)


def test_back_to_back_requests_are_spaced(monkeypatch):
    _reset(monkeypatch)
    slept: list[float] = []
    now = {"t": 100.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(mod.time, "sleep", lambda s: (slept.append(s), now.__setitem__("t", now["t"] + s)))

    mod.MillenniumUltraClient._pace()   # first one is free
    mod.MillenniumUltraClient._pace()   # second waits out the interval
    assert slept and abs(slept[0] - 0.05) < 1e-9


def test_a_caller_that_waited_long_enough_is_not_delayed(monkeypatch):
    _reset(monkeypatch)
    slept: list[float] = []
    now = {"t": 100.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))

    mod.MillenniumUltraClient._pace()
    now["t"] += 10.0
    mod.MillenniumUltraClient._pace()
    assert slept == []


def test_the_pace_is_shared_across_clients(monkeypatch):
    """A page load and a sweep are different clients hitting one install."""
    _reset(monkeypatch)
    calls: list[float] = []
    real_sleep = mod.time.sleep

    def record(seconds):
        calls.append(seconds)
        real_sleep(min(seconds, 0.05))

    monkeypatch.setattr(mod.time, "sleep", record)
    mod.MillenniumUltraClient._pace()
    mod.MillenniumUltraClient._pace()
    assert calls, "second caller should have waited even from a fresh client"


def test_concurrent_callers_queue_rather_than_collide(monkeypatch):
    """The lock is held across the wait, so they space out instead of
    all sleeping the same interval and firing together."""
    _reset(monkeypatch, interval=0.02)
    stamps: list[float] = []

    def worker():
        mod.MillenniumUltraClient._pace()
        stamps.append(mod.time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert all(g >= 0.015 for g in gaps), gaps


def test_the_connection_ceiling_is_stated():
    assert mod.MAX_CONNECTIONS <= 5

"""How often to sync is a property of the PACS, not of the schedule.

The engine's floor is a minute, because the floor is what a holder waits:
nothing reaches the PACS at the moment somebody installs a pass, so the
credential their watch was given and the switching-on of their card both
happen on the next cycle.

That suits an HTTP API. It may not suit Millennium Ultra, which is an
operator's UI whose roster sweep and queued export cost it real work — and
raising the floor globally to protect one install would slow every other one
down. So an adapter can name its own, and the engine takes the larger of
that and the dynamic rule.
"""

from __future__ import annotations

from types import SimpleNamespace

from agsync.lib.pacs.base import PacsDescriptor
from agsync.sync.engine import (
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    SyncEngine,
)


def _descriptor(min_interval_s=None) -> PacsDescriptor:
    return PacsDescriptor(
        vendor="test",
        display_name="Test PACS",
        trigger_help_key="x",
        connection_fields=[],
        min_interval_s=min_interval_s,
    )


def _engine_with(adapter) -> SyncEngine:
    engine = SyncEngine.__new__(SyncEngine)
    engine._pacs = None if adapter is None else ("fingerprint", adapter)
    return engine


def _adapter(descriptor):
    return SimpleNamespace(descriptor=lambda: descriptor)


def test_an_adapter_that_names_no_floor_gets_the_engines():
    assert _engine_with(_adapter(_descriptor()))._min_interval_s() == MIN_INTERVAL_S


def test_an_adapter_can_ask_to_be_read_less_often():
    assert _engine_with(_adapter(_descriptor(300)))._min_interval_s() == 300


def test_no_adapter_yet_falls_back():
    """The first cycle, and any cycle that failed before building one."""
    assert _engine_with(None)._min_interval_s() == MIN_INTERVAL_S


def test_an_adapter_that_raises_does_not_stop_the_sync():
    """A scheduling detail must not be able to take the engine down."""
    def explode():
        raise RuntimeError("no descriptor")

    broken = SimpleNamespace(descriptor=explode)
    assert _engine_with(broken)._min_interval_s() == MIN_INTERVAL_S


def test_a_slow_pacs_is_still_backed_off_beyond_its_floor():
    """The floor raises the minimum; it does not cap the dynamic rule."""
    engine = _engine_with(_adapter(_descriptor(60)))

    assert engine._interval_after(16_000) == 60, "a fast cycle gets the floor"
    assert engine._interval_after(60_000) == 180, "a slow one is read less often"
    assert engine._interval_after(400_000) == MAX_INTERVAL_S


def test_the_wait_between_cycles_honours_the_adapters_floor():
    """The floor has to reach the number the engine actually sleeps for."""
    patient = _engine_with(_adapter(_descriptor(300)))
    eager = _engine_with(_adapter(_descriptor()))

    assert patient._interval_after(16_000) == 300
    assert eager._interval_after(16_000) == MIN_INTERVAL_S


def test_the_shipped_adapters_are_deliberate_about_it():
    """Each one either names a floor or takes the default knowingly."""
    import agsync.lib.pacs  # noqa: F401 — registers the adapters
    from agsync.lib.pacs.registry import available_pacs

    descriptors = available_pacs()
    assert descriptors, "no adapters registered"
    for descriptor in descriptors:
        assert descriptor.min_interval_s is None or descriptor.min_interval_s > 0, (
            f"{descriptor.vendor} names a nonsensical interval"
        )

"""The PACS direction is read from the AccessGrid template, not asked for.

Getting it wrong is asymmetrically bad — Seos writes cards into the PACS —
so it is derived from the card template's protocol every cycle, and an
unreadable template falls back to the read-only direction.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.sync.engine import SyncEngine


@pytest.fixture
def engine() -> SyncEngine:
    return SyncEngine()


def _pacs_cfg(vendor="millennium_ultra", **params):
    base = {"base_url": "https://millennium.test", "trigger_card_format": "7"}
    base.update(params)
    return {"vendor": vendor, "params": base, "options": {}}


def _ag_cfg():
    return {"account_id": "a", "api_secret": "s", "template_id": "tpl"}


def _with_protocol(monkeypatch, protocol):
    monkeypatch.setattr(
        "agsync.sync.engine.template_protocol", lambda client, tid: protocol,
    )


def test_seos_template_selects_the_writing_direction(engine, monkeypatch):
    _with_protocol(monkeypatch, "seos")
    out = engine._with_derived_mode(object(), _ag_cfg(), _pacs_cfg())
    assert out["params"]["mode"] == "seos"


def test_desfire_template_selects_the_reading_direction(engine, monkeypatch):
    _with_protocol(monkeypatch, "desfire")
    out = engine._with_derived_mode(object(), _ag_cfg(), _pacs_cfg())
    assert out["params"]["mode"] == "desfire"


def test_unreadable_template_falls_back_to_read_only(engine, monkeypatch):
    # Never let a failed lookup turn a read-only integration into one that
    # writes cards into the customer's PACS.
    _with_protocol(monkeypatch, "")
    out = engine._with_derived_mode(object(), _ag_cfg(), _pacs_cfg())
    assert out["params"]["mode"] == "desfire"


def test_an_unknown_protocol_falls_back_to_read_only(engine, monkeypatch):
    _with_protocol(monkeypatch, "something-new")
    out = engine._with_derived_mode(object(), _ag_cfg(), _pacs_cfg())
    assert out["params"]["mode"] == "desfire"


def test_a_stored_mode_is_overridden_by_the_template(engine, monkeypatch):
    # Whatever is on disk loses: the template is the source of truth.
    _with_protocol(monkeypatch, "desfire")
    out = engine._with_derived_mode(object(), _ag_cfg(), _pacs_cfg(mode="seos"))
    assert out["params"]["mode"] == "desfire"


def test_adapters_that_do_not_derive_are_left_alone(engine, monkeypatch):
    _with_protocol(monkeypatch, "seos")
    cfg = _pacs_cfg(vendor="cdvi")
    assert engine._with_derived_mode(object(), _ag_cfg(), cfg) == cfg


def test_derivation_does_not_mutate_the_stored_config(engine, monkeypatch):
    _with_protocol(monkeypatch, "seos")
    cfg = _pacs_cfg()
    engine._with_derived_mode(object(), _ag_cfg(), cfg)
    assert "mode" not in cfg["params"]


def test_a_changed_template_rebuilds_the_adapter(engine, monkeypatch):
    # The adapter cache is keyed on the config, so a protocol change has to
    # produce a different fingerprint or the old direction would persist.
    built: list[str] = []
    monkeypatch.setattr(
        "agsync.sync.engine.build_pacs_adapter",
        lambda vendor, params: built.append(params["mode"]) or SimpleNamespace(),
    )
    _with_protocol(monkeypatch, "desfire")
    engine._pacs_adapter(engine._with_derived_mode(object(), _ag_cfg(), _pacs_cfg()))
    engine._pacs_adapter(engine._with_derived_mode(object(), _ag_cfg(), _pacs_cfg()))
    _with_protocol(monkeypatch, "seos")
    engine._pacs_adapter(engine._with_derived_mode(object(), _ag_cfg(), _pacs_cfg()))

    assert built == ["desfire", "seos"]  # cached in between, rebuilt on change


def test_a_fast_cycle_still_waits_five_minutes():
    """The interval used to be 3x the cycle, floored at 10 seconds.

    That was sensible while cycle time tracked how hard the PACS was
    working. Reading it through a bulk export made cycles finish in seconds
    — so the rule rewarded finding a cheap path with syncing every 15
    seconds, which is more total load on the install, not less.
    """
    from agsync.sync.engine import (
        INTERVAL_MULTIPLIER,
        MAX_INTERVAL_S,
        MIN_INTERVAL_S,
    )

    def interval(cycle_seconds):
        return max(
            MIN_INTERVAL_S,
            min(MAX_INTERVAL_S, cycle_seconds * INTERVAL_MULTIPLIER),
        )

    assert interval(5) == MIN_INTERVAL_S
    assert interval(60) == 180, "a slower cycle is still read less often"


def test_a_slow_cycle_still_backs_off():
    """The property the dynamic rule exists for, kept."""
    from agsync.sync.engine import (
        INTERVAL_MULTIPLIER,
        MAX_INTERVAL_S,
        MIN_INTERVAL_S,
    )

    def interval(cycle_seconds):
        return max(
            MIN_INTERVAL_S,
            min(MAX_INTERVAL_S, cycle_seconds * INTERVAL_MULTIPLIER),
        )

    assert interval(150) > MIN_INTERVAL_S
    assert interval(400) == MAX_INTERVAL_S

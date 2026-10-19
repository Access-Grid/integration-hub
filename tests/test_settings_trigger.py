"""Saving the enrollment trigger stores its id and its display name.

The setup screens choose a format by id; the export the sync reads prints
its name. Both are stored at save time so a cycle never has to look up one
from the other.
"""

from __future__ import annotations

import pytest

from agsync.routes import connect
from tests._fakes import FakeMillenniumClient


@pytest.fixture
def formats(monkeypatch, make_millennium_adapter):
    adapter = make_millennium_adapter(FakeMillenniumClient())
    monkeypatch.setattr(connect, "build_adapter", lambda vendor, params: adapter)
    monkeypatch.setattr(connect, "_configured_vendor", lambda: "millennium_ultra")


def test_both_the_id_and_the_name_are_stored(formats):
    params = connect.trigger_params({"base_url": "https://m.test"}, " 7 ")
    assert params == {
        "base_url": "https://m.test",
        "trigger_card_format": "7",
        "trigger_card_format_label": "HID 37",
    }


def test_a_format_the_pacs_does_not_define_is_refused(formats):
    with pytest.raises(ValueError, match="not defined"):
        connect.trigger_params({}, "99")

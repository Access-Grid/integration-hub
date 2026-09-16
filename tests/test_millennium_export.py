"""The bulk cardholder export, and what it is allowed to decide.

Reading every cardholder's detail page to find who carries a trigger card
costs ~1,600 requests; on the live install that is 6 slots out of 4,797.
The export answers the same question in three. What it must never do is
decide anything on its own: it has no cardholder id, and names collide
badly enough here that a name match is a shortlist to confirm, not an
identification.
"""

from __future__ import annotations

import io
import pathlib
import zipfile

import pytest

from agsync.lib.pacs.millennium_ultra import export
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS
from tests._fakes import FakeMillenniumClient

TRIGGER_LABEL = "AccessGrid / HID Wallet Format"
FIXTURE = pathlib.Path(__file__).parent / "millennium_fixtures" / "cardholder_export.csv"


@pytest.fixture
def csv_text():
    return FIXTURE.read_text()


# --- reading the file ----------------------------------------------------


def test_the_archive_is_one_csv(csv_text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Cardholders.csv", csv_text)
    assert export.unpack(buf.getvalue()).splitlines()[0].startswith("First Name")


def test_an_archive_without_a_csv_is_an_error():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "nothing useful")
    with pytest.raises(ValueError, match="No CSV"):
        export.unpack(buf.getvalue())


def test_slots_are_folded_up_per_cardholder(csv_text):
    people = export.parse(csv_text)
    auston = next(p for p in people if p.last_name == "Bunsen")
    assert [(s.index, s.facility_code, s.card_number, s.active) for s in auston.slots] == [
        (1, "1", "1", False),
        (2, "2", "1255", True),
        (3, "2", "1256", False),
    ]


def test_an_empty_slot_is_not_a_card(csv_text):
    """Every slot occupies its columns whether or not it holds a card."""
    people = export.parse(csv_text)
    ordinary = next(p for p in people if p.last_name == "Sandoval")
    assert len(ordinary.slots) == 1


def test_the_trigger_format_is_matched_by_its_display_name(csv_text):
    people = export.parse(csv_text)
    carrying = [p for p in people if p.carries_format(TRIGGER_LABEL)]
    assert sorted(p.last_name for p in carrying) == ["Bunsen", "Grid"]


def test_the_join_key_ignores_case_and_padding():
    assert export.name_key(" Auston ", "BUNSEN") == ("auston", "bunsen")


# --- what it is allowed to decide ----------------------------------------


class ExportingClient(FakeMillenniumClient):
    def __init__(self, csv_text, roster, pages, fail=None):
        super().__init__(roster=roster, pages=pages, formats=[("8", TRIGGER_LABEL)])
        self._csv = csv_text
        self._fail = fail
        self.exports = 0

    def export_cardholders(self):
        self.exports += 1
        if self._fail:
            raise self._fail
        return self._csv


def _roster():
    return [
        {"ID": 11591, "IsActive": True, "Name": "Bunsen, Auston"},
        {"ID": 11618, "IsActive": True, "Name": "Grid, Access"},
        {"ID": 197, "IsActive": True, "Name": "Sandoval, Diego"},
    ]


def _adapter(make_millennium_adapter, millennium_page, client_cls=ExportingClient, **kw):
    csv_text = FIXTURE.read_text()
    client = client_cls(csv_text, _roster(), {"11591": millennium_page}, **kw)
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    adapter.trigger_card_format = "8"
    return adapter, client


def test_only_the_cardholders_carrying_a_trigger_card_are_read(
    make_millennium_adapter, millennium_page
):
    adapter, client = _adapter(make_millennium_adapter, millennium_page)
    list(adapter.list_people())
    # Diego Sandoval is in the roster and the export, and carries no trigger
    # card — so his detail page is never fetched.
    assert adapter._sweep == {"11591", "11618"}
    assert client.exports == 1


def test_a_failed_export_falls_back_to_sweeping(
    make_millennium_adapter, millennium_page
):
    """An optimisation, not a dependency — a PACS that will not export is
    still a PACS that can be synced."""
    adapter, _ = _adapter(
        make_millennium_adapter, millennium_page, fail=RuntimeError("502"),
    )
    list(adapter.list_people())
    assert adapter._sweep == {"11591", "11618", "197"}  # cold: everyone


def test_an_ambiguous_name_shortlists_every_candidate(
    make_millennium_adapter, millennium_page, caplog
):
    """Names are not unique here — one collides five ways on the live
    install — so every candidate is read and confirmed rather than guessed
    between.

    And it is said out loud: otherwise more pages get read than there are
    trigger cards, with nothing explaining the difference.
    """
    csv_text = FIXTURE.read_text()
    roster = _roster() + [{"ID": 9999, "IsActive": True, "Name": "Bunsen, Auston"}]
    client = ExportingClient(csv_text, roster, {"11591": millennium_page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    adapter.trigger_card_format = "8"
    with caplog.at_level("INFO"):
        list(adapter.list_people())
    assert {"11591", "9999"} <= adapter._sweep
    assert "Auston Bunsen matches 2 cardholders by name" in caplog.text
    # Two people carry a trigger card; three roster entries might be them.
    assert "2 cardholder(s) carrying the trigger format, across 3" in caplog.text


def test_an_unambiguous_run_reports_one_number(
    make_millennium_adapter, millennium_page, caplog
):
    """Counting candidates as cardholders would overstate the usual case."""
    adapter, _ = _adapter(make_millennium_adapter, millennium_page)
    with caplog.at_level("INFO"):
        list(adapter.list_people())
    assert "export found 2 cardholder(s) carrying the trigger format" in caplog.text
    assert "across" not in caplog.text
    assert "matches" not in caplog.text


def test_a_trigger_card_with_no_roster_match_is_reported(
    make_millennium_adapter, millennium_page, caplog
):
    """The one case the export is worse than the sweep: somebody who should
    be enrolled and cannot be placed."""
    csv_text = FIXTURE.read_text()
    roster = [r for r in _roster() if r["ID"] != 11618]
    client = ExportingClient(csv_text, roster, {"11591": millennium_page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    adapter.trigger_card_format = "8"
    with caplog.at_level("WARNING"):
        list(adapter.list_people())
    assert "no roster entry matches" in caplog.text


def test_a_cardholder_outside_the_sweep_is_never_fetched(
    make_millennium_adapter, millennium_page
):
    """The bug the export exposed.

    The gate only skipped cardholders that were already cached, so anyone
    never read fell through and was fetched regardless of the sweep. That
    was invisible while the first sweep read everybody; once the export
    narrowed the sweep to the two who carry a trigger card, the other 1,597
    were still being read one page at a time.
    """
    csv_text = FIXTURE.read_text()
    pages = {"11591": millennium_page, "11618": millennium_page, "197": millennium_page}
    client = ExportingClient(csv_text, _roster(), pages)
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    adapter.trigger_card_format = "8"

    list(adapter.list_people())
    fetched = []
    original = client.get_cardholder_form

    def spy(cardholder_id):
        fetched.append(str(cardholder_id))
        return original(cardholder_id)

    client.get_cardholder_form = spy
    for pid in ("11591", "11618", "197"):
        adapter._profile_for(pid)

    assert "197" not in fetched, "swept out, so it must not be read"
    assert sorted(fetched) == ["11591", "11618"]


def test_an_unread_cardholder_reports_unreadable_rather_than_empty(
    make_millennium_adapter, millennium_page
):
    """Not a failure — the normal answer for most of the roster now.

    It has its own exception so the snapshot can count these quietly
    instead of logging a warning per cardholder, which on this install
    would be ~1,597 lines a cycle.
    """
    from agsync.lib.pacs import PacsRecordUnavailable

    csv_text = FIXTURE.read_text()
    client = ExportingClient(csv_text, _roster(), {"11591": millennium_page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    adapter.trigger_card_format = "8"
    list(adapter.list_people())

    with pytest.raises(PacsRecordUnavailable, match="not read this cycle"):
        list(adapter.list_credentials("197"))


def test_the_snapshot_counts_unread_records_instead_of_warning_per_person(
    make_millennium_adapter, millennium_page, caplog
):
    """One line, not one per cardholder.

    Most of the roster is deliberately unread now, so a warning each turned
    a healthy cycle into ~1,597 lines of alarm and buried everything worth
    seeing.
    """
    from types import SimpleNamespace

    from agsync.sync.snapshot import build_snapshot

    csv_text = FIXTURE.read_text()
    pages = {"11591": millennium_page, "11618": millennium_page, "197": millennium_page}
    client = ExportingClient(csv_text, _roster(), pages)
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    adapter.trigger_card_format = "8"

    ag = SimpleNamespace(access_cards=SimpleNamespace(list=lambda **kw: []))
    with caplog.at_level("INFO"):
        snap = build_snapshot(adapter, ag, "tmpl-1")

    assert "197" not in snap.credentials_by_person   # unread, so absent
    assert "Failed to fetch credentials" not in caplog.text
    assert "1 record(s) not read this cycle" in caplog.text

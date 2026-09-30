"""Two cardholders with one name, and the export that cannot tell them apart.

The export carries no cardholder id, so its rows are joined to the roster by
name — and on this install 42% of names collide, one of them five ways. That
join decides which detail pages to read, which is safe: every candidate is
read and only the one actually holding a trigger card yields a credential.

It also used to decide whose email and phone we sent to AccessGrid, which was
not safe. Each row's details were written against every roster entry sharing
the name, so the last row won and one namesake's pass went out addressed to
the other. That is unrecoverable — a pass's email cannot be changed once it
is issued, and the cycle a cardholder is first seen is the cycle they are
provisioned on.
"""

from __future__ import annotations

import re

from agsync.lib.pacs.base import CredentialIdentity
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
TRIGGER_LABEL = "HID 37"

_HEADER = (
    "First Name,Last Name,Employee ID,E-Mail,Phone,"
    "Card 1 Encoded Card No.,Card 1 Facility Code,"
    "Card 1 Card Format,Card 1 Active"
)


def _csv(*rows: str) -> str:
    return "\n".join((_HEADER, *rows)) + "\n"


class ExportingClient(FakeMillenniumClient):
    """A client that serves a bulk export, so the name join is exercised."""

    def __init__(self, csv: str, **kw):
        super().__init__(**kw)
        self._csv = csv
        self._formats = [("1", "Wiegand Card"), (TRIGGER, TRIGGER_LABEL)]

    def export_cardholders(self) -> str:
        return self._csv


def _page(millennium_page, set_slot, *, trigger: bool, email="", phone=""):
    """A cardholder whose slot 1 holds a trigger card, or does not."""
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number="1", facility_code="99",
        card_format=TRIGGER if trigger else "1", active=True,
    )
    for index in (2, 3):
        page = set_slot(page, index, card_id="", card_number="", card_format=None)
    for field, value in (("EMail", email), ("Phone", phone)):
        page = re.sub(
            r'(<input\b[^>]*\bname="' + field + r'"[^>]*\bvalue=")[^"]*(")',
            lambda m, v=value: m.group(1) + v + m.group(2),
            page,
            count=1,
        )
    return page


def _namesakes(make_millennium_adapter, millennium_page, set_slot, *, carrying):
    """Two Ann Lees. `carrying` names the one holding the trigger card."""
    pages = {
        "100": _page(
            millennium_page, set_slot, trigger=carrying == "100",
            email="ann.one@real.test", phone="111",
        ),
        "200": _page(
            millennium_page, set_slot, trigger=carrying == "200",
            email="ann.two@real.test", phone="222",
        ),
    }
    client = ExportingClient(
        _csv(
            f"Ann,Lee,1,ann.one@export.test,111,1,99,{TRIGGER_LABEL},True",
            f"Ann,Lee,2,ann.two@export.test,222,1,99,{TRIGGER_LABEL},True",
        ),
        roster=[
            {"ID": "100", "IsActive": True, "Name": "Lee, Ann"},
            {"ID": "200", "IsActive": True, "Name": "Lee, Ann"},
        ],
        pages=pages,
    )
    adapter = make_millennium_adapter(
        client, mode=MODE_SEOS, trigger_card_format=TRIGGER,
    )
    return adapter, client


def test_each_namesake_gets_their_own_contact_details(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The regression. Neither Ann is handed the other's email or phone."""
    adapter, _ = _namesakes(
        make_millennium_adapter, millennium_page, set_slot, carrying="100",
    )
    people = {p.id: p for p in adapter.list_people()}

    assert people["100"].email == "ann.one@real.test"
    assert people["100"].phone == "111"
    assert people["200"].email == "ann.two@real.test"
    assert people["200"].phone == "222"


def test_a_shared_name_is_never_taken_from_the_export(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The export cannot say whose details these are, so it does not say."""
    adapter, _ = _namesakes(
        make_millennium_adapter, millennium_page, set_slot, carrying="100",
    )
    list(adapter.list_people())

    assert "100" not in adapter._exported_contact
    assert "200" not in adapter._exported_contact


def test_a_unique_name_still_comes_from_the_export(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The fallback still does its job where the name identifies somebody.

    This is what it exists for: on the cycle a cardholder is first seen
    their detail page has not been read, and the pass goes out that cycle.
    """
    client = ExportingClient(
        _csv(f"Bob,Stone,9,bob@export.test,999,1,99,{TRIGGER_LABEL},True"),
        roster=[{"ID": "300", "IsActive": True, "Name": "Stone, Bob"}],
        pages={"300": _page(millennium_page, set_slot, trigger=True)},
    )
    adapter = make_millennium_adapter(
        client, mode=MODE_SEOS, trigger_card_format=TRIGGER,
    )
    people = {p.id: p for p in adapter.list_people()}

    assert adapter._exported_contact["300"] == ("bob@export.test", "999")
    assert people["300"].email == "bob@export.test"
    assert people["300"].phone == "999"


def test_only_the_namesake_holding_a_trigger_card_gets_a_credential(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Both pages are read; the card decides, not the name."""
    adapter, _ = _namesakes(
        make_millennium_adapter, millennium_page, set_slot, carrying="200",
    )
    list(adapter.list_people())

    assert adapter._sweep == {"100", "200"}
    assert list(adapter.list_credentials("100")) == []
    assert len(list(adapter.list_credentials("200"))) == 1


def test_a_card_lands_on_the_namesake_it_was_issued_for(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Everything past the name join is keyed by cardholder id."""
    adapter, client = _namesakes(
        make_millennium_adapter, millennium_page, set_slot, carrying="200",
    )
    list(adapter.list_people())

    assert adapter.write_back_credentials(
        "200", "seos", [CredentialIdentity("99", "47")]
    ) is True
    assert [cid for cid, _ in client.saved] == ["200"]
    assert SeosLedger.get("200", "seos")
    assert SeosLedger.get("100", "seos") == []


def test_reading_a_namesakes_page_early_is_not_an_extra_request(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The page is cached, so the read `list_credentials` does is the same one."""
    adapter, _ = _namesakes(
        make_millennium_adapter, millennium_page, set_slot, carrying="100",
    )
    list(adapter.list_people())
    read_after_roster = adapter._read_this_cycle

    # A second cycle: both profiles are cached, so nothing is read early.
    list(adapter.list_people())
    assert adapter._read_this_cycle == 0
    assert read_after_roster == 2

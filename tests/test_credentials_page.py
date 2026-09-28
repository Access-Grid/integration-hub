"""The credentials page reports what actually reached the PACS.

For a Seos install this is the only place that half of the sync is visible:
the AccessGrid pass exists whether or not its card numbers were written, and
that difference is a pass that opens doors versus one that does not.
"""

from __future__ import annotations

import pytest

from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient


@pytest.fixture
def adapter(make_millennium_adapter, millennium_page):
    client = FakeMillenniumClient(
        roster=[{"ID": 11587, "Name": "Grid, Accessg"}],
        pages={"11587": millennium_page},
    )
    return make_millennium_adapter(client, mode=MODE_SEOS)


def test_written_card_numbers_are_reported(adapter, seos_ledger):
    SeosLedger.record("11587", "seos", [
        {"slot": 2, "card_number": "1216", "facility_code": "2"},
        {"slot": 3, "card_number": "1217", "facility_code": "2"},
    ])
    assert adapter.written_credentials() == {("11587", "seos"): ["1216", "1217"]}


def test_nothing_written_reports_nothing(adapter, seos_ledger):
    assert adapter.written_credentials() == {}


def test_several_cardholders_are_kept_apart(adapter, seos_ledger):
    SeosLedger.record("11587", "seos",
                      [{"slot": 2, "card_number": "1216", "facility_code": "2"}])
    SeosLedger.record("8824", "seos",
                      [{"slot": 3, "card_number": "5001", "facility_code": "66"}])
    written = adapter.written_credentials()
    assert written[("11587", "seos")] == ["1216"]
    assert written[("8824", "seos")] == ["5001"]


def test_a_read_only_adapter_reports_nothing(make_millennium_adapter, millennium_page):
    # DESFire copies cards out; nothing is ever written, so the column has
    # nothing to say rather than saying "not saved" about every row.
    client = FakeMillenniumClient(roster=[], pages={"11587": millennium_page})
    desfire = make_millennium_adapter(client)
    assert desfire.supports_credential_writeback is False


def test_the_page_renders_both_states(millennium_page, seos_ledger, monkeypatch):
    """Render the template directly: one row written, one not."""
    from types import SimpleNamespace

    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("src/agsync/templates"), autoescape=True)
    # base.html expects the request/nav context; render the fragment we own.
    template = env.get_template("credentials.html")
    template.environment.globals["t"] = lambda key, **kw: (
        f"{kw.get('count','')} saved to {kw.get('pacs','')}".strip()
        if key == "credentials.saved_to_pacs"
        else f"not saved to {kw.get('pacs','')}" if key == "credentials.not_in_pacs"
        else key
    )
    html = template.render(
        credentials=[
            SimpleNamespace(full_name="Accessg Grid", employee_id="11587",
                            status="active", ag_card_id="I_Ugc",
                            pacs_person_id="11587", pacs_credential_id="seos"),
            SimpleNamespace(full_name="Other Person", employee_id="8824",
                            status="active", ag_card_id="xyz",
                            pacs_person_id="8824", pacs_credential_id="seos"),
        ],
        written={("11587", "seos"): ["1216", "1217"]},
        pacs_name="Millennium Ultra (MGI)",
        configured=True, admin_exists=True, locale="en", available_locales=["en"],
    )
    assert "2 saved to Millennium Ultra (MGI)" in html
    assert "not saved to Millennium Ultra (MGI)" in html
    # The numbers themselves are on the badge for looking up a failing pass.
    assert 'title="1216, 1217"' in html


def test_the_page_derives_the_direction_rather_than_reading_stored_params(monkeypatch):
    """The mode is not in the saved params — the engine derives it per cycle.

    Building an adapter from the raw params gets the read-only default, so
    the page reported every pass as unwritten no matter what the ledger said.
    """
    from agsync.sync.engine import derived_pacs_params

    monkeypatch.setattr(
        "agsync.sync.engine.template_protocol", lambda client, tid: "seos",
    )
    stored = {"vendor": "millennium_ultra", "params": {"base_url": "https://m.test"}}
    assert "mode" not in stored["params"]

    resolved = derived_pacs_params(object(), {"template_id": "tpl"}, stored)
    assert resolved["params"]["mode"] == "seos"
    # And the stored config is left alone.
    assert "mode" not in stored["params"]

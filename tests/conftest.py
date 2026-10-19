"""Shared fixtures for the adapter test suites.

Each vendor's payloads mirror the real shapes its system returns, captured
from a live tenant or controller and trimmed to the fields the adapter
reads — Avigilon Alta first, then CDVI Atrium and Millennium Ultra below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _never_the_real_database(tmp_path, monkeypatch):
    """Point every test at a database of its own.

    Without this a test that reaches storage reads the operator's live
    install: `get_settings()` defaults db_path to it, so `SeosLedger.all()`
    answers with real cardholders and `AccessGridConfig.load()` with a real
    API key. That is how test_only_the_cardholders_carrying_a_trigger_card
    came to depend on run order — it passed only when some earlier test had
    left the shared connection pointed somewhere harmless.

    The connection has to be dropped properly for the new path to take.
    `connection` keeps `_initialized` and a thread-local `conn`; there is
    no `_conn`, so the line several suites use to reset it sets an
    attribute nobody reads.
    """
    from agsync.config import get_settings
    from agsync.db import connection

    def drop() -> None:
        conn = getattr(connection._local, "conn", None)
        if conn is not None:
            conn.close()
            del connection._local.conn
        connection._initialized = False

    monkeypatch.setenv("AG_SYNC_DB_PATH", str(tmp_path / "test.db"))
    get_settings.cache_clear()
    drop()
    yield
    drop()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _allowed_to_act(monkeypatch):
    """Let the suite exercise what this install does when it may act.

    Dry run is the shipped default, so without this every test asserting
    that something was written would be asserting against a no-op. The
    default, and the stored setting behind it, are pinned in test_dry_run.py
    rather than here, so flipping either cannot go unnoticed.
    """
    from agsync import dryrun

    monkeypatch.setattr(dryrun, "active", lambda: False)
    yield
    dryrun.invalidate()


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def sample_user() -> dict:
    """An Alta user as returned by GET /orgs/{org}/users."""
    return {
        "id": 39734661,
        "status": "A",
        "externalId": "accessgrid",
        "title": None,
        "department": None,
        "identity": {
            "fullName": "Auston Bunsen",
            "firstName": "Auston",
            "lastName": "Bunsen",
            "email": "ab@accessgrid.com",
            "mobilePhone": None,
        },
    }


@pytest.fixture
def wiegand_cred(now) -> dict:
    """A Wiegand-ID credential (credentialType id 2), active (endDate future)."""
    return {
        "id": 58943905,
        "startDate": _iso(now - timedelta(days=1)),
        "endDate": _iso(now + timedelta(days=200)),
        "credentialType": {"id": 2, "name": "Card: Wiegand ID", "modelName": "card"},
        "eventAction": None,
        "badgeConfig": None,
        "card": {
            "numBits": 26,
            "fields": {"facilityCode": "69", "cardId": "42069"},
            "id": 11554705,
            "number": "11732486708497743872",
            "isOutputEnabled": False,
            "facilityCode": "69",
            "cardId": "42069",
            "cardFormat": {"id": 5150, "code": "prox26std", "numBits": 26},
        },
    }


@pytest.fixture
def mobile_cred() -> dict:
    """A non-card credential (Mobile, id 1) — must be filtered out."""
    return {
        "id": 99999,
        "startDate": None,
        "endDate": None,
        "credentialType": {"id": 1, "name": "Mobile", "modelName": "mobile"},
        "card": None,
    }


@pytest.fixture
def make_adapter():
    """Build an AvigilonAltaAdapter with its client swapped for a fake."""
    from agsync.lib.pacs.avigilon_alta.adapter import AvigilonAltaAdapter

    def _build(fake_client) -> AvigilonAltaAdapter:
        adapter = AvigilonAltaAdapter(email="x@y.com", password="pw")
        adapter._client.close()  # close the real httpx client built in __init__
        adapter._client = fake_client
        return adapter

    return _build


# --- CDVI Atrium fixtures ------------------------------------------------
#
# These mirror the dict shape node_to_hash produces from a live Atrium
# controller's users.xml and cards.xml (all attribute values are strings).
# Real shapes: users.xml rows use `state` for the enable flag and carry no
# email; cards.xml rows use `en` and nest their assigned <USER> element
# (parsed to card["USER"]). The `number` on a card is CDVI's encoded hex:
# high byte = site code, low two bytes = card number. 0x45 = 69 (site),
# 0x00E4 = 228 (card). The enrollment trigger is the `[accessgrid...]`
# marker in the card's `name` (Display Name).


@pytest.fixture
def cdvi_user() -> dict:
    """An Atrium user as returned by users.xml."""
    return {
        "id": "5",
        "fn": "Amy",
        "ln": "Hyatt",
        "state": "1",
        "al0": "0",
    }


@pytest.fixture
def cdvi_card() -> dict:
    """A card assigned to user 5, enrolled via its Display Name marker."""
    return {
        "id": "77",
        "name": "Amy iPhone [accessgrid-apple]",
        "number": "00000000004500e4",  # site 69, card 228 (lowercase hex)
        "format": "0",
        "en": "1",
        "lost": "0",
        "stolen": "0",
        "USER": {"id": "5", "fn": "Amy", "ln": "Hyatt"},
    }


@pytest.fixture
def make_cdvi_adapter():
    """Build a CdviAdapter with its client swapped for a fake."""
    from agsync.lib.pacs.cdvi.adapter import CdviAdapter

    def _build(fake_client) -> CdviAdapter:
        adapter = CdviAdapter(base_url="https://ctrl.test", username="u", password="p")
        adapter._client.close()  # close the real httpx client built in __init__
        adapter._client = fake_client
        return adapter

    return _build


# --- Millennium Ultra fixtures -------------------------------------------
#
# The adapter reads only the export, built per test with tests._fakes
# export_row. The cardholder page is the real thing, captured from a live
# install (see tests/millennium_fixtures), and is kept for the client tests.


@pytest.fixture
def millennium_page() -> str:
    from pathlib import Path

    return (
        Path(__file__).parent / "millennium_fixtures" / "cardholder_11587_form.html"
    ).read_text(encoding="utf-8")


@pytest.fixture
def make_millennium_adapter():
    """Build a MillenniumUltraAdapter with its client swapped for a fake."""
    from agsync.lib.pacs.millennium_ultra.adapter import MillenniumUltraAdapter

    def _build(fake_client, migrated=True, **kwargs) -> MillenniumUltraAdapter:
        if migrated:
            from agsync.lib.pacs.millennium_ultra import migrate
            from agsync.settings_store import set_json

            set_json(migrate.DONE_KEY, True)
        kwargs.setdefault("base_url", "https://millennium.test")
        kwargs.setdefault("trigger_card_format", "7")
        kwargs.setdefault("trigger_card_format_label", "HID 37")
        kwargs.setdefault("email_domain", "cards.example.com")
        kwargs.setdefault("auth_cookie", "test-cookie")
        adapter = MillenniumUltraAdapter(**kwargs)
        adapter._client.close()  # close the real httpx client built in __init__
        adapter._client = fake_client
        return adapter

    return _build


@pytest.fixture
def seos_ledger(monkeypatch):
    """In-memory stand-in for the encrypted settings blob the ledger uses.

    The ledger records which Millennium slots hold AccessGrid-allocated
    cards; the tests need its behaviour, not SQLite.
    """
    store: dict = {}

    def fake_get(key):
        return store.get(key)

    def fake_set(key, value):
        store[key] = value

    monkeypatch.setattr("agsync.settings_store.get_json", fake_get, raising=False)
    monkeypatch.setattr("agsync.settings_store.set_json", fake_set, raising=False)
    return store

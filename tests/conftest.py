"""Shared fixtures for the Avigilon Alta test suite.

The sample payloads mirror the real shapes returned by the Helium/OpenPath
API (captured from a live tenant), trimmed to the fields the adapter reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


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
# These mirror the dict shape node_to_hash produces from Atrium's users.xml
# and cards.xml (all attribute values are strings). The `number` on a card
# is CDVI's encoded hex: high byte = site code, low two bytes = card number.
# 0x45 = 69 (site code), 0xA455 = 42069 (card number). The enrollment
# trigger is the `[accessgrid...]` marker in the card's `name` (Display Name).


@pytest.fixture
def cdvi_user() -> dict:
    """An Atrium user as returned by users.xml."""
    return {
        "id": "5",
        "fn": "Amy",
        "ln": "Hyatt",
        "email": "amy@example.com",
        "en": "1",
    }


@pytest.fixture
def cdvi_card() -> dict:
    """A card assigned to user 5, enrolled via its Display Name marker."""
    return {
        "id": "77",
        "user_id": "5",
        "name": "Amy iPhone [accessgrid-apple]",
        "number": "000000000045A455",
        "en": "1",
        "lost": "0",
        "stolen": "0",
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

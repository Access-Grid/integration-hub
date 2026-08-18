"""Test doubles shared across the Avigilon Alta suite."""

from __future__ import annotations


class FakeAltaClient:
    """Stand-in for AltaClient — no network, records write calls."""

    def __init__(self, users=None, creds_by_user=None):
        self._users = users or []
        self._creds_by_user = creds_by_user or {}
        self.patch_calls: list[dict] = []
        self.patch_result = True

    def list_users(self):
        return self._users

    def list_credentials(self, user_id):
        return self._creds_by_user.get(str(user_id), [])

    def patch_credential_dates(self, user_id, credential_id, **kwargs):
        self.patch_calls.append({"user_id": user_id, "credential_id": credential_id, **kwargs})
        return self.patch_result


class FakeCdviClient:
    """Stand-in for CdviClient — no network, no crypto, no writes.

    Returns already-parsed record dicts (the shape node_to_hash produces
    from users.xml / cards.xml), so the adapter's mapping logic is exercised
    without going near the encrypted XML protocol.
    """

    def __init__(self, users=None, cards=None, emails=None):
        self._users = users or []
        self._cards = cards or []
        self._emails = emails or {}  # user_id -> email (from the SDK cfg2 read)
        self.set_enabled_calls: list[dict] = []
        self.card_command_result = True

    def test_connection(self):
        return True, ""

    def list_users(self):
        return self._users

    def list_cards(self):
        return self._cards

    def get_user_email(self, user_id):
        return self._emails.get(str(user_id), "")

    def set_card_enabled(self, card, enabled):
        self.set_enabled_calls.append({"card_id": str(card.get("id")), "enabled": enabled})
        return self.card_command_result

    def close(self):
        pass


class FakeMillenniumUltraClient:
    """Stand-in for MillenniumUltraClient — no network.

    Serves a roster of {"ID": ...} rows and a {cardholder_id: html} map, which
    is exactly what the real client returns, so the adapter's mapping and
    trigger logic run against real page markup.
    """

    def __init__(self, roster=None, pages=None, company="ICON"):
        self._roster = roster or []
        self._pages = pages or {}
        self.company = company
        self.roster_calls = 0
        self.page_calls: list[list[str]] = []

    def list_roster(self):
        self.roster_calls += 1
        return self._roster

    def get_cardholder_html(self, cardholder_id):
        return self._pages[str(cardholder_id)]

    def get_cardholders_html(self, ids):
        self.page_calls.append(list(ids))
        return {pid: self._pages[pid] for pid in ids if pid in self._pages}

    def test_connection(self):
        if not self._roster:
            return False, f"Connected as {self.company}, but no cardholders are visible to this account."
        return True, f"Connected as {self.company} — {len(self._roster):,} cardholders found."

    def close(self):
        pass

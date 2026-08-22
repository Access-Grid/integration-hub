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


class FakeMillenniumClient:
    """Stand-in for MillenniumUltraClient — no network, records writes.

    Serves the real captured cardholder page (optionally mutated per
    cardholder) so the adapter's slot reading and form mutation run against
    genuine markup rather than a hand-written approximation.
    """

    def __init__(self, roster=None, pages=None, formats=None):
        self._roster = roster or []
        self._pages = pages or {}  # cardholder id -> html
        self._formats = formats or [("1", "Wiegand Card"), ("7", "HID 37")]
        self.saved: list[tuple[str, object]] = []
        self.validated: list[dict] = []
        self.save_result = True
        self.number_free = True

    def list_cardholders(self):
        return self._roster

    def first_cardholder_id(self):
        return str(self._roster[0]["ID"]) if self._roster else ""

    def get_cardholder_form(self, cardholder_id):
        from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

        return CardholderForm.parse(self._pages[str(cardholder_id)])

    def card_formats(self, cardholder_id):
        return self._formats

    def card_number_is_free(self, slot, card_number, facility_code, card_format):
        self.validated.append({
            "slot": slot, "card_number": card_number,
            "facility_code": facility_code, "card_format": card_format,
        })
        return self.number_free

    def save_cardholder(self, cardholder_id, form):
        self.saved.append((str(cardholder_id), form))
        return self.save_result

    def test_connection(self):
        return True, "ok"

    def close(self):
        pass

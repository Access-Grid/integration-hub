"""Test doubles shared across the PACS adapter suites."""

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


# The export's columns, in Millennium's order.
EXPORT_COLUMNS = [
    "First Name", "Last Name", "Card Type", "Employee ID", "Phone", "E-Mail",
    "Current Status",
] + [
    f"Card {n} {field}"
    for n in (1, 2, 3)
    for field in (
        "Printed Card No.", "Encoded Card No.", "Activation Date",
        "Expiration Date", "Active", "Card Format", "Facility Code", "Badge Type",
    )
]


def export_row(employee_id, first="Ada", last="Lovelace", email="", phone="", slots=()):
    """One cardholder as an export row.

    `slots` is a list of dicts, one per slot from slot 1: card_number,
    facility_code, card_format (the display name), active, activation and
    expiration (export date text). An omitted slot, or None, is empty.
    """
    row = {
        "Employee ID": employee_id, "First Name": first, "Last Name": last,
        "E-Mail": email, "Phone": phone,
    }
    for n, slot in enumerate(slots, start=1):
        if not slot:
            continue
        row[f"Card {n} Encoded Card No."] = slot.get("card_number", "")
        row[f"Card {n} Facility Code"] = slot.get("facility_code", "")
        row[f"Card {n} Card Format"] = slot.get("card_format", "")
        row[f"Card {n} Active"] = "True" if slot.get("active", True) else "False"
        row[f"Card {n} Activation Date"] = slot.get("activation", "")
        row[f"Card {n} Expiration Date"] = slot.get("expiration", "")
    return row


class FakeMillenniumClient:
    """Stand-in for MillenniumUltraClient — no network.

    Serves `rows` (see export_row) as the export CSV, so the adapter reads
    them through the real export.parse.
    """

    def __init__(self, rows=None, formats=None):
        self.rows = list(rows or [])
        self._formats = formats or [("1", "Wiegand Card"), ("7", "HID 37")]

    def export_cardholders(self):
        import csv
        import io

        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS, restval="")
        writer.writeheader()
        writer.writerows(self.rows)
        return out.getvalue()

    def first_cardholder_id(self):
        return "1"

    def card_formats(self, cardholder_id):
        return self._formats

    def test_connection(self):
        return True, "ok"

    def close(self):
        pass

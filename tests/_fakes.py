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

    def __init__(self, roster=None, pages=None, formats=None, export=False):
        self._roster = roster or []
        # Off unless asked for, so a suite about the A-Z sweep keeps getting
        # the sweep. The adapter only falls back to it when the export is
        # unavailable, and that fallback is what those tests are about.
        self._export = export
        self._pages = pages or {}  # cardholder id -> html
        self._formats = formats or [("1", "Wiegand Card"), ("7", "HID 37")]
        self.saved: list[tuple[str, object]] = []
        self.validated: list[dict] = []
        self.save_result = True
        self.number_free = True
        self.deleted: list[tuple[str, str]] = []
        self.delete_result = True
        # Import rows, and the CSV each call would have uploaded.
        self.imported: list[list] = []
        self.imported_csv: list[str] = []
        self.import_result = True
        self.import_log = ""
        # Detail-page reads, and a hook fired on each one. The write path
        # reads twice — once to plan, once again immediately before the
        # import — so a test can stand in for an operator editing the record
        # in between by swapping `_pages` from here.
        self.page_reads: list[str] = []
        self.on_read = None

    def list_cardholders(self):
        return self._roster

    def first_cardholder_id(self):
        return str(self._roster[0]["ID"]) if self._roster else ""

    def get_cardholder_form(self, cardholder_id):
        from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

        pid = str(cardholder_id)
        self.page_reads.append(pid)
        if self.on_read is not None:
            self.on_read(self, pid, len(self.page_reads))
        return CardholderForm.parse(self._pages[pid])

    def card_formats(self, cardholder_id):
        return self._formats

    def card_number_is_free(self, slot, card_number, facility_code, card_format):
        self.validated.append({
            "slot": slot, "card_number": card_number,
            "facility_code": facility_code, "card_format": card_format,
        })
        return self.number_free

    # The real export's columns, in the order the install emits them. The
    # fixed trio comes first and is what the import matches a row on.
    _EXPORT_SLOT_COLUMNS = (
        "Printed Card No.", "Encoded Card No.", "Activation Date",
        "Expiration Date", "Active", "Card Format", "Facility Code",
        "Badge Type",
    )

    def export_cardholders(self):
        """A CSV in the shape the real export produces, from this fake's own
        roster and pages.

        Worth deriving rather than stubbing: the adapter builds its "which
        names identify exactly one cardholder" map out of the export, and the
        write path refuses to act on a name that map does not carry. A fake
        that returned nothing would make every write in every test fail for a
        reason that has nothing to do with what the test is about — and a fake
        that returned a fixed string would let a genuine name collision pass
        unnoticed.

        Names come from the roster, parsed the same way the adapter parses
        them, so the join by name is the real one. Slots come from the
        served page, so a cardholder carrying the trigger format in the
        fixture carries it in the export too.
        """
        if not self._export:
            raise RuntimeError("this fake has no export (pass export=True)")
        import csv as _csv
        import io as _io

        from agsync.lib.pacs.millennium_ultra.adapter import (
            DATE_FORMAT,
            _read_slots,
            parse_roster_name,
        )
        from agsync.lib.pacs.millennium_ultra.client import CARD_SLOTS
        from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

        labels = dict(self._formats)
        header = [
            "First Name", "Last Name", "Card Type", "Employee ID", "Phone",
            "E-Mail", "Current Status",
        ]
        for index in CARD_SLOTS:
            header += [f"Card {index} {c}" for c in self._EXPORT_SLOT_COLUMNS]

        buffer = _io.StringIO(newline="")
        writer = _csv.DictWriter(
            buffer, fieldnames=header, restval="", lineterminator="\r\n"
        )
        writer.writeheader()
        for entry in self._roster:
            pid = str(entry["ID"])
            first, last = parse_roster_name(entry.get("Name", ""))
            record = {"First Name": first, "Last Name": last, "Card Type": ""}
            html = self._pages.get(pid)
            if html:
                form = CardholderForm.parse(html)
                # The key the import matches rows on. Carried because the
                # adapter counts it to find duplicates, and a fake that
                # always left it blank would make that check untestable.
                record["Employee ID"] = form.value("EmployeeID").strip()
                for slot in _read_slots(form, 0):
                    if slot.empty:
                        continue
                    record[f"Card {slot.index} Encoded Card No."] = slot.card_number
                    record[f"Card {slot.index} Facility Code"] = slot.facility_code
                    # The export prints the format's display name, not the id.
                    record[f"Card {slot.index} Card Format"] = labels.get(
                        slot.card_format, ""
                    )
                    record[f"Card {slot.index} Active"] = str(slot.active)
                    if slot.activation:
                        record[f"Card {slot.index} Activation Date"] = (
                            slot.activation.strftime(DATE_FORMAT)
                        )
                    if slot.expiration:
                        record[f"Card {slot.index} Expiration Date"] = (
                            slot.expiration.strftime(DATE_FORMAT)
                        )
            writer.writerow(record)
        return buffer.getvalue()

    def import_cardholders(self, rows):
        """Record the rows, and the CSV they render to.

        The CSV is built with the real writer so a test can assert on the
        file that would go on the wire — which is the whole point of the
        import path being a pure function of its rows.
        """
        from agsync.lib.pacs.millennium_ultra import import_rows as _import_rows
        from agsync.lib.pacs.millennium_ultra.client import ImportOutcome

        rows = list(rows)
        self.imported.append(rows)
        self.imported_csv.append(_import_rows.to_csv(rows) if rows else "")
        return ImportOutcome(
            ok=self.import_result, rows=len(rows), log=self.import_log
        )

    def save_cardholder(self, cardholder_id, form):
        self.saved.append((str(cardholder_id), form))
        return self.save_result

    def delete_card(self, cardholder_id, card_id, token):
        self.deleted.append((str(cardholder_id), str(card_id)))
        return self.delete_result

    def test_connection(self):
        return True, "ok"

    def close(self):
        pass


def written_slot(client, slot: int, row: int = -1):
    """The slot write the last import carried for `slot`, or None.

    None means the slot was not in the file at all — which under a diff is
    exactly what "left alone" looks like, there being no round-trip to
    preserve. That makes it a stronger statement than the old form-based
    equivalent: a field absent from the file cannot be written wrongly,
    whereas a field echoed back could be.
    """
    assert client.imported, "expected an import"
    for write in client.imported[row][-1].slots:
        if write.index == slot:
            return write
    return None

"""One-time move from Millennium record ids to Employee IDs.

Installs from before the export keyed everything by Millennium's record id:
the tracking table, the Seos ledger. The export carries no record id, so
without this every tracked cardholder would vanish from the snapshot and
phase 3 would delete every pass.

Each tracked record's page is read once for its Employee ID, then every key
moves in one transaction. Until that has fully succeeded the cycle fails, so
nothing runs against half-moved keys. After it, no page is read again.

A record that no longer exists in Millennium cannot move. Its rows are
keyed `gone:<record id>`, which no export row matches, so phase 3 revokes its
pass as it would for any deleted cardholder.

It also fills in the trigger format's display name, which older installs
never stored.
"""

from __future__ import annotations

import logging

from ....db.connection import get_db
from .client import MillenniumAuthError, MillenniumError

logger = logging.getLogger(__name__)

DONE_KEY = "millennium_keyed_by_employee_id"


def ensure(adapter) -> None:
    """Run the migration unless it already has; raise until it succeeds."""
    from ....settings_store import PacsConfig, get_json, set_json

    if get_json(DONE_KEY):
        return

    if not adapter._trigger_label:
        label = dict(adapter.card_formats()).get(adapter.trigger_card_format, "")
        if label:
            PacsConfig.update_params(trigger_card_format_label=label)
            adapter._trigger_label = label

    db = get_db()
    record_ids = {
        row["pacs_person_id"]
        for table in ("ag_credentials", "pacs_cards")
        for row in db.execute(f"SELECT DISTINCT pacs_person_id FROM {table}")
    }

    employee_ids: dict[str, str] = {}
    gone: list[str] = []
    problems = 0
    for record_id in sorted(record_ids):
        try:
            form = adapter._client.get_cardholder_form(record_id)
        except MillenniumAuthError:
            raise
        except Exception as e:  # noqa: BLE001 — reported, and the migration waits
            logger.error("Millennium: cannot read cardholder %s to migrate it: %s", record_id, e)
            problems += 1
            continue
        page_id = form.value("ID").strip()
        if page_id != record_id:
            # Millennium answers a deleted record with an empty cardholder
            # form rather than an error; a real one carries its own id.
            logger.warning(
                "Millennium: cardholder record %s no longer exists (its page has ID %r) — "
                "its pass will be revoked like any deleted cardholder's",
                record_id, page_id,
            )
            gone.append(record_id)
            continue
        employee_id = form.value("EmployeeID").strip()
        if not employee_id:
            logger.error("Millennium: cardholder %s has no Employee ID to migrate to", record_id)
            problems += 1
            continue
        employee_ids[record_id] = employee_id

    holders: dict[str, list[str]] = {}
    for record_id, employee_id in employee_ids.items():
        holders.setdefault(employee_id, []).append(record_id)
    for employee_id, ids in holders.items():
        if len(ids) > 1:
            logger.error(
                "Millennium: cardholders %s share Employee ID %s — cannot migrate them",
                ", ".join(ids), employee_id,
            )
            problems += 1

    if problems:
        raise MillenniumError(
            f"{problems} cardholder(s) cannot move to Employee IDs yet — fix them in "
            "Millennium; nothing syncs until they are"
        )

    db.execute("BEGIN")
    try:
        # Through a prefix, so a record id that equals somebody's Employee ID
        # is never overwritten mid-move.
        for table in ("ag_credentials", "pacs_cards"):
            db.execute(f"UPDATE {table} SET pacs_person_id = 'migrating:' || pacs_person_id")
        for record_id, employee_id in employee_ids.items():
            db.execute(
                "UPDATE ag_credentials SET pacs_person_id = ?, employee_id = ? "
                "WHERE pacs_person_id = ?",
                (employee_id, employee_id, f"migrating:{record_id}"),
            )
            db.execute(
                "UPDATE pacs_cards SET pacs_person_id = ? WHERE pacs_person_id = ?",
                (employee_id, f"migrating:{record_id}"),
            )
        for record_id in gone:
            for table in ("ag_credentials", "pacs_cards"):
                db.execute(
                    f"UPDATE {table} SET pacs_person_id = ? WHERE pacs_person_id = ?",
                    (f"gone:{record_id}", f"migrating:{record_id}"),
                )
        set_json(DONE_KEY, True)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    for record_id, employee_id in sorted(employee_ids.items()):
        logger.info(
            "Millennium: cardholder record %s is now Employee ID %s", record_id, employee_id,
        )
    logger.info(
        "Millennium: moved %d cardholder(s) from record ids to Employee IDs",
        len(employee_ids),
    )

"""One-time move from Millennium record ids to Employee IDs.

Installs from before the export keyed everything by Millennium's record id:
the tracking table, the Seos ledger. The export carries no record id, so
without this every tracked cardholder would vanish from the snapshot and
phase 3 would delete every pass.

Each tracked record's page is read once for its Employee ID, then every key
moves in one transaction. Until that has fully succeeded the cycle fails, so
nothing runs against half-moved keys. After it, no page is read again.

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
    from .adapter import SeosLedger

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
        for row in db.execute("SELECT DISTINCT pacs_person_id FROM ag_credentials")
    } | {key.partition(":")[0] for key in SeosLedger.all()}

    employee_ids: dict[str, str] = {}
    problems = 0
    for record_id in sorted(record_ids):
        try:
            employee_id = adapter._client.get_cardholder_form(record_id).value("EmployeeID").strip()
        except MillenniumAuthError:
            raise
        except Exception as e:  # noqa: BLE001 — reported, and the migration waits
            logger.error("Millennium: cannot read cardholder %s to migrate it: %s", record_id, e)
            problems += 1
            continue
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

    ledger = SeosLedger.all()
    moved_ledger: dict[str, list] = {}
    for key, entries in ledger.items():
        record_id, _, credential_id = key.partition(":")
        moved_ledger[f"{employee_ids[record_id]}:{credential_id}"] = entries

    db.execute("BEGIN")
    try:
        # Through a prefix, so a record id that equals somebody's Employee ID
        # is never overwritten mid-move.
        db.execute("UPDATE ag_credentials SET pacs_person_id = 'migrating:' || pacs_person_id")
        for record_id, employee_id in employee_ids.items():
            db.execute(
                "UPDATE ag_credentials SET pacs_person_id = ?, employee_id = ? "
                "WHERE pacs_person_id = ?",
                (employee_id, employee_id, f"migrating:{record_id}"),
            )
        set_json(SeosLedger.KEY, moved_ledger)
        set_json(DONE_KEY, True)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    logger.info(
        "Millennium: moved %d cardholder(s) from record ids to Employee IDs",
        len(employee_ids),
    )

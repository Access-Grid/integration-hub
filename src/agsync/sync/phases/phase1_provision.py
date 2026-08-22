"""Phase 1 — Provision new credentials (PACS → AccessGrid).

For every PACS credential whose `trigger_active` is True and that we
have not already provisioned, create a new AccessGrid card. We record
the result in the tracking table.

Skip rules (with reasons logged):
  - person inactive
  - person has no email and no phone (AG needs a delivery channel)
  - person has no full_name
  - tracking row exists with a non-NULL ag_card_id (already provisioned)

Most PACS hand us a card that already exists and we copy its identity to
AccessGrid. A credential flagged `allocate_identity` runs the other way:
AccessGrid mints the facility code and card number (it allocates whenever
they are omitted), and we hand the result straight back to the adapter to
write into the PACS. Dedupe is skipped for those — there is no identity yet
to collide with.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ...ag import AccessGrid, AccessGridError
from ...lib.pacs import PacsAdapter
from .. import tracking
from ..snapshot import Snapshot
from .writeback import push_allocated_identities

logger = logging.getLogger(__name__)

# Most cycles provision a handful of people. A number far above that means
# something changed that shouldn't have — a mistyped trigger, a trigger that
# suddenly matches a common card format — and on a site with thousands of
# cardholders that runs away fast. Capping it turns a runaway into something
# slow and visible: the rest are picked up on later cycles, and the operator
# gets a warning naming the number while there is still time to stop it.
MAX_PROVISIONS_PER_CYCLE = 25


def run(
    snapshot: Snapshot,
    ag: AccessGrid,
    template_id: str,
    site_code: str = "",
    dedupe_by_site_card: bool = False,
    extra_metadata: dict | None = None,
    use_file_data: bool = False,
    card_title: str = "",
    card_classification: str = "",
    pacs: PacsAdapter | None = None,
    should_stop: Callable[[], bool] | None = None,
    max_per_cycle: int = MAX_PROVISIONS_PER_CYCLE,
) -> int:
    provisioned = 0
    skipped = 0
    deduped = 0
    eligible = 0
    logger.info(
        "Phase 1: Checking for new credentials to provision (dedupe=%s)",
        "on" if dedupe_by_site_card else "off",
    )

    for pid, creds in snapshot.credentials_by_person.items():
        person = snapshot.people.get(pid)
        if person is None or not person.active:
            continue
        if should_stop is not None and should_stop():
            logger.warning("Phase 1: configuration changed — stopping early")
            break
        for cred in creds:
            if not cred.trigger_active:
                skipped += 1
                continue

            # Per-credential site code (Alta) wins over the global setting.
            eff_site_code = cred.site_code or site_code

            existing = tracking.get(pid, cred.id)
            if existing and existing.ag_card_id:
                # Already provisioned (or already deduped) — keep tracking row fresh.
                if existing.status == "deduped":
                    continue
                ag_card = snapshot.ag_cards_by_token.get((pid, cred.id))
                if ag_card is None:
                    ag_card = snapshot.ag_card_by_id.get(existing.ag_card_id)
                ag_state = (getattr(ag_card, "state", "") or "").lower() if ag_card else ""
                if ag_state and ag_state != existing.last_known_ag_state:
                    tracking.update_last_known_ag_state(pid, cred.id, ag_state)
                continue

            if not person.full_name:
                logger.warning("  %s: no name — skipping", pid)
                skipped += 1
                continue
            if not person.email and not person.phone:
                logger.warning("  %s (%s): no email or phone — skipping", pid, person.full_name)
                skipped += 1
                continue

            # Multi-instance dedupe: if another sync tool (or a prior cycle on
            # this one) has already provisioned an AG card for this physical
            # credential, don't double-provision.
            if (
                dedupe_by_site_card
                and not cred.allocate_identity
                and eff_site_code
                and cred.card_number
            ):
                key = (str(eff_site_code), str(cred.card_number))
                hit = snapshot.ag_cards_by_site_card.get(key)
                if hit is not None:
                    existing_id = getattr(hit, "id", None)
                    logger.info(
                        "  Dedupe: AG card %s already exists for site=%s card=%s — skipping %s (%s)",
                        existing_id, eff_site_code, cred.card_number, person.full_name, pid,
                    )
                    tracking.mark_deduped(
                        pacs_person_id=pid,
                        pacs_credential_id=cred.id,
                        existing_ag_card_id=existing_id,
                        full_name=person.full_name,
                    )
                    deduped += 1
                    continue

            eligible += 1
            if provisioned >= max_per_cycle:
                # Deliberately after the tracking row would have been written
                # so nothing is half-recorded; these are simply retried next
                # cycle, by which time an operator has had a chance to look.
                continue

            now = datetime.now(UTC)
            start_date = (cred.activate_date or now).isoformat()
            expiration_date = (cred.deactivate_date or (now + timedelta(days=365))).isoformat()

            # Start from the user-configured extras, then layer the
            # sync-managed keys on top so they always win the merge.
            metadata: dict = dict(extra_metadata or {})
            metadata["pacs_credential_id"] = cred.id
            if not cred.allocate_identity:
                if eff_site_code:
                    metadata["site_code"] = eff_site_code
                if cred.card_number:
                    metadata["card_number"] = str(cred.card_number)

            params: dict = {
                "card_template_id": template_id,
                "employee_id": pid,
                "full_name": person.full_name,
                "start_date": start_date,
                "expiration_date": expiration_date,
                "metadata": metadata,
            }
            # Wire format: either the pre-encoded file_data blob (opt-in, when
            # the adapter supplies one) or the decoded site_code + card_number
            # (default). site_code/card_number stay in metadata either way so
            # dedupe and debugging still work.
            if cred.allocate_identity:
                # Send no identity at all: AccessGrid allocates the facility
                # code and card number, and the adapter writes them back.
                pass
            elif use_file_data and cred.file_data:
                params["file_data"] = cred.file_data
            else:
                if eff_site_code and eff_site_code.isdigit():
                    params["site_code"] = int(eff_site_code)
                if cred.card_number:
                    params["card_number"] = cred.card_number
            if person.email:
                params["email"] = person.email
            if person.phone:
                params["phone_number"] = person.phone
            # A per-person title from the PACS wins; the configured one
            # fills in for systems that have no such field, which is most.
            title = person.title or card_title
            if title:
                params["title"] = title
            if card_classification:
                params["classification"] = card_classification

            # Insert tracking row in 'pending' state before the API call so a
            # failed provision still leaves a row for phase 5 to retry.
            tracking.upsert(
                pacs_person_id=pid,
                pacs_credential_id=cred.id,
                ag_card_id=None,
                full_name=person.full_name,
                employee_id=pid,
                status="pending",
                last_synced_email=person.email,
                last_synced_phone=person.phone,
                last_synced_full_name=person.full_name,
                last_synced_title=person.title,
            )

            try:
                logger.info("  Provisioning: %s (%s)", person.full_name, pid)
                card = ag.access_cards.provision(**params)
                ag_card_id = getattr(card, "id", None)
                ag_state = (getattr(card, "state", "active") or "active").lower()
                tracking.upsert(
                    pacs_person_id=pid,
                    pacs_credential_id=cred.id,
                    ag_card_id=ag_card_id,
                    full_name=person.full_name,
                    employee_id=pid,
                    status="active" if ag_state in ("active", "created") else ag_state,
                    last_synced_email=person.email,
                    last_synced_phone=person.phone,
                    last_synced_full_name=person.full_name,
                    last_synced_title=person.title,
                    last_known_ag_state=ag_state,
                )
                provisioned += 1
                logger.info("  Provisioned AG card %s for %s", ag_card_id, person.full_name)
                if cred.allocate_identity and pacs is not None:
                    # Hand the freshly-minted identity straight back so the
                    # card exists in the PACS before the next cycle reads it.
                    push_allocated_identities(pacs, pid, cred.id, card)
            except AccessGridError as e:
                logger.error("  Provision failed for %s: %s", person.full_name, e)
                tracking.record_error(pid, cred.id, str(e))
            except Exception as e:  # noqa: BLE001
                logger.error("  Unexpected error provisioning %s: %s", person.full_name, e)
                tracking.record_error(pid, cred.id, f"{type(e).__name__}: {e}")

    if eligible > max_per_cycle:
        logger.warning(
            "Phase 1: %d credentials are eligible but this cycle provisions at "
            "most %d. If that number is unexpected, check the enrollment "
            "trigger before the remaining %d are provisioned on later cycles.",
            eligible, max_per_cycle, eligible - provisioned,
        )
    logger.info(
        "Phase 1 done: %d provisioned, %d skipped, %d deduped",
        provisioned, skipped, deduped,
    )
    return provisioned

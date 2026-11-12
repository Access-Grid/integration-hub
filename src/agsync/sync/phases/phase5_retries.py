"""Phase 5 — Retry failed provisions.

Walks the tracking table for rows with sync_error set and retry_count
below MAX_RETRIES. Re-runs the same provision logic as phase 1.

Tracking rows that exceed MAX_RETRIES are left alone with their error
message visible to the operator on the status page.

Credentials flagged `allocate_identity` follow the same rule as in phase 1:
AccessGrid mints the identity, dedupe is skipped, and the result is handed
back to the adapter to write into the PACS.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ... import dryrun
from ...ag import AccessGrid, AccessGridError
from ...lib.pacs import CredentialStatus, PacsAdapter
from .. import tracking
from ..snapshot import Snapshot
from .phase1_provision import note_issued_card, refuse_duplicate, unrecorded_pass
from .writeback import push_allocated_identities

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


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
    default_pass_days: int = 365,
    require_unique_card: bool = False,
) -> int:
    _settle_exhausted(snapshot)
    failed = tracking.failed_records(MAX_RETRIES)
    if not failed:
        logger.debug("Phase 5: No failed records to retry")
        return 0

    if not snapshot.ag_cards_read:
        logger.warning("Phase 5: AccessGrid's pass list was not read — retrying nothing this cycle")
        return 0

    logger.info("Phase 5: Retrying %d failed provision(s)", len(failed))
    succeeded = 0

    for tracked in failed:
        if (tracked.pacs_person_id, tracked.pacs_credential_id) in snapshot.issue_attempted:
            # Phase 1 tried it moments ago. If that call failed after
            # AccessGrid created the pass, only the next cycle's listing
            # shows it — retrying now would issue a second.
            logger.info(
                "  %s/%s: phase 1 tried it this cycle — retried next cycle, once the "
                "listing can show whether that issue went through",
                tracked.pacs_person_id, tracked.pacs_credential_id,
            )
            continue
        person = snapshot.people.get(tracked.pacs_person_id)
        creds = snapshot.credentials_by_person.get(tracked.pacs_person_id, [])
        cred = next((c for c in creds if c.id == tracked.pacs_credential_id), None)
        if cred is not None and cred.hold:
            continue

        if person is None or cred is None or not cred.trigger_active:
            # Underlying record is gone or no longer eligible — clear retry.
            tracking.remove(tracked.pacs_person_id, tracked.pacs_credential_id)
            continue

        if not person.email and not person.phone:
            tracking.record_error(
                tracked.pacs_person_id, tracked.pacs_credential_id,
                "still missing email and phone",
            )
            continue

        # Per-credential site code (Alta) wins over the global setting.
        eff_site_code = cred.site_code or site_code

        if (
            dedupe_by_site_card
            and not cred.allocate_identity
            and eff_site_code
            and cred.card_number
        ):
            hit = snapshot.ag_cards_by_site_card.get(
                (str(eff_site_code), str(cred.card_number))
            )
            if hit is not None:
                existing_id = getattr(hit, "id", None)
                logger.info(
                    "  Dedupe: AG card %s already exists for site=%s card=%s — marking %s deduped",
                    existing_id, eff_site_code, cred.card_number, person.full_name,
                )
                tracking.mark_deduped(
                    pacs_person_id=tracked.pacs_person_id,
                    pacs_credential_id=tracked.pacs_credential_id,
                    existing_ag_card_id=existing_id,
                    full_name=person.full_name,
                )
                continue
        if require_unique_card and refuse_duplicate(snapshot, tracked.pacs_person_id, person.full_name,
                                                    cred, eff_site_code):
            tracking.record_error(tracked.pacs_person_id, tracked.pacs_credential_id,
                                  "card number already on another pass")
            continue
        if unrecorded_pass(snapshot, tracked.pacs_person_id, person.full_name, cred):
            tracking.record_error(tracked.pacs_person_id, tracked.pacs_credential_id,
                                  "AccessGrid holds a pass for this card the sync has no record of")
            continue

        now = datetime.now(UTC)
        metadata: dict = dict(extra_metadata or {})
        # Our own handle on this issue, echoed back on every card the
        # call produces — including both halves of a template pair, which
        # is what makes them findable in a later listing.
        sync_ref = tracking.new_sync_ref()
        metadata["sync_ref"] = sync_ref
        metadata["pacs_credential_id"] = cred.id
        if not cred.allocate_identity:
            if eff_site_code:
                metadata["site_code"] = eff_site_code
            if cred.card_number:
                metadata["card_number"] = str(cred.card_number)

        params: dict = {
            "card_template_id": template_id,
            "employee_id": tracked.pacs_person_id,
            "full_name": person.full_name,
            "start_date": (cred.activate_date or now).isoformat(),
            "expiration_date": (cred.deactivate_date or (now + timedelta(days=default_pass_days))).isoformat(),
            "metadata": metadata,
        }
        # Same wire-format choice as phase 1: verbatim file_data (opt-in) or
        # decoded site_code + card_number (default); site/card stay metadata.
        if cred.allocate_identity:
            # AccessGrid allocates when the identity is omitted.
            pass
        elif cred.file_data and (use_file_data or cred.send_file_data):
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
        # A per-person title from the PACS wins; the configured one fills in
        # for systems that have no such field, which is most of them.
        title = person.title or card_title
        if title:
            params["title"] = title
        if person.department:
            params["department"] = person.department
        if card_classification:
            params["classification"] = card_classification

        try:
            logger.info(
                "  Retry attempt for %s/%s (attempt %d/%d)",
                tracked.pacs_person_id, tracked.pacs_credential_id,
                tracked.retry_count + 1, MAX_RETRIES,
            )
            if dryrun.active():
                dryrun.would(
                    "retry provisioning %s/%s",
                    tracked.pacs_person_id, tracked.pacs_credential_id,
                )
                continue
            snapshot.issue_attempted.add((tracked.pacs_person_id, tracked.pacs_credential_id))
            card = ag.access_cards.provision(**params)
            ag_card_id = getattr(card, "id", None)
            ag_state = (getattr(card, "state", "active") or "active").lower()
            tracking.upsert(
                pacs_person_id=tracked.pacs_person_id,
                pacs_credential_id=tracked.pacs_credential_id,
                ag_card_id=ag_card_id,
                full_name=person.full_name,
                employee_id=tracked.pacs_person_id,
                status="active" if ag_state in ("active", "created") else ag_state,
                last_synced_email=person.email,
                last_synced_phone=person.phone,
                last_synced_full_name=person.full_name,
                last_synced_title=person.title,
                last_synced_department=person.department,
                last_known_ag_state=ag_state,
                sync_ref=sync_ref,
            )
            succeeded += 1
            logger.info("  Retry succeeded for %s", person.full_name)
            note_issued_card(snapshot, cred, eff_site_code, card)
            if cred.allocate_identity and pacs is not None:
                push_allocated_identities(
                    pacs, tracked.pacs_person_id, tracked.pacs_credential_id, card,
                )
        except AccessGridError as e:
            logger.warning("  Retry failed for %s: %s", person.full_name, e)
            tracking.record_error(tracked.pacs_person_id, tracked.pacs_credential_id, str(e))
        except Exception as e:  # noqa: BLE001
            logger.warning("  Retry crashed for %s: %s", person.full_name, e)
            tracking.record_error(
                tracked.pacs_person_id, tracked.pacs_credential_id,
                f"{type(e).__name__}: {e}",
            )

    logger.info("Phase 5 done: %d/%d succeeded", succeeded, len(failed))
    return succeeded


def _settle_exhausted(snapshot: Snapshot) -> None:
    """Cards whose issue failed MAX_RETRIES times: mailed, or forgotten.

    Forgotten once the PACS no longer wants a pass for the card — switched
    off, its trigger taken away, or gone — so switching it back on is a
    fresh start with fresh tries. Otherwise the operator is told it will not
    be tried again (notifications mails each card at most once a while).
    A cardholder not read this cycle says nothing either way.
    """
    from ... import notifications

    for tracked in tracking.exhausted_records(MAX_RETRIES):
        pid, cid = tracked.pacs_person_id, tracked.pacs_credential_id
        if pid not in snapshot.credentials_by_person:
            continue
        cred = next((c for c in snapshot.credentials_by_person[pid] if c.id == cid), None)
        person = snapshot.people.get(pid)
        if (
            cred is None
            or not cred.trigger_active
            or cred.status is not CredentialStatus.ACTIVE
            or (person is not None and not person.active)
        ):
            logger.info(
                "  %s: no longer marked for an active pass — forgetting its %d failed "
                "issue(s); marking it again starts over",
                tracking.label(tracked), tracked.retry_count,
            )
            tracking.remove(pid, cid)
            continue
        problem = (
            f"{tracked.full_name or pid} ({pid}) has card {cid} marked for a pass, but AccessGrid "
            f"refused to issue it {tracked.retry_count} times, most recently: {tracked.sync_error}. "
            f"It will not be tried again. Fix the cause, then switch the card off and back on "
            f"in the PACS to try again."
        )
        logger.warning("  %s", problem)
        notifications.notify_card_problem(f"issue-failed:{pid}:{cid}", problem)

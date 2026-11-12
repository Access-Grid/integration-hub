"""Phase 1 — Provision new credentials (PACS → AccessGrid).

For every PACS credential whose `trigger_active` is True and that we
have not already provisioned, create a new AccessGrid card. We record
the result in the tracking table.

Skip rules (with reasons logged):
  - person inactive
  - person has no email and no phone (AG needs a delivery channel)
  - person has no full_name
  - credential past its expiry date (phase 3 would delete the pass at once)
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

from ... import dryrun
from ...ag import AccessGrid, AccessGridError
from ...lib.pacs import PacsAdapter
from .. import tracking
from ..snapshot import Snapshot
from .phase3_deletions import expired
from .writeback import push_allocated_identities

logger = logging.getLogger(__name__)

# Most cycles provision a handful of people. A number far above that means
# something changed that shouldn't have — a mistyped trigger, a trigger that
# suddenly matches a common card format — and on a site with thousands of
# cardholders that runs away fast. Capping it turns a runaway into something
# slow and visible: the rest are picked up on later cycles, and the operator
# gets a warning naming the number while there is still time to stop it.
MAX_PROVISIONS_PER_CYCLE = 25


def _card_key(cred, eff_site_code: str) -> tuple[str, str] | None:
    """(site code, card number) as card metadata indexes it; None for a card AccessGrid allocates."""
    if cred.allocate_identity or not cred.card_number:
        return None
    return (str(eff_site_code or ""), str(cred.card_number))


def refuse_duplicate(snapshot: Snapshot, pid: str, name: str, cred, eff_site_code: str) -> bool:
    """Is this card's number already on a live pass? If so, say so and mail it.

    For a PACS whose card numbers must be unique (descriptor
    unique_card_numbers). A deleted or unlinked pass carries the number of
    nobody's working card, so it does not count.
    """
    from ... import notifications
    from .writeback import is_dead

    key = _card_key(cred, eff_site_code)
    hit = snapshot.ag_cards_by_site_card.get(key) if key else None
    if hit is None or is_dead(hit):
        return False
    problem = (
        f"{name} ({pid}) has card {key[0] or '-'}/{key[1]} ({cred.id}) marked for a pass, but AccessGrid pass "
        f"{getattr(hit, 'id', '?')} (employee {getattr(hit, 'employee_id', '?')}) already carries that number "
        f"— not issuing a second. Correct the card number in the PACS."
    )
    logger.error("  %s", problem)
    notifications.notify_card_problem(f"duplicate:{pid}:{cred.id}", problem)
    return True


def unrecorded_pass(snapshot: Snapshot, pid: str, name: str, cred) -> bool:
    """Does AccessGrid already hold a live pass for this card that we have no record of?

    Asked just before issuing, when no tracking row holds a pass for it. A
    live pass carrying this person and credential is then one an earlier
    issue created whose reply never arrived — the call failed, the pass did
    not. Issuing again would send the holder a second invite and leave the
    first pass beyond the sync's reach. It is not adopted: for a template
    pair the listing shows only the halves, never the pair id we act on. So
    it is held and mailed, until someone deletes that pass in AccessGrid.
    """
    from ... import notifications
    from .writeback import is_dead

    live = [c for c in snapshot.ag_cards_by_token.get((pid, cred.id), []) if not is_dead(c)]
    if not live:
        return False
    ids = ", ".join(f"{getattr(c, 'id', '?')} ({getattr(c, 'state', '') or '?'})" for c in live)
    problem = (
        f"{name} ({pid}) has card {cred.id} marked for a pass, but AccessGrid already holds "
        f"{'pass' if len(live) == 1 else 'passes'} {ids} for it that the sync has no record of — most "
        f"likely an earlier issue whose reply never arrived. Not issuing another. Delete "
        f"{'that pass' if len(live) == 1 else 'those passes'} in AccessGrid to have a new one issued."
    )
    logger.error("  %s", problem)
    notifications.notify_card_problem(f"unrecorded:{pid}:{cred.id}", problem)
    return True


def note_issued_card(snapshot: Snapshot, cred, eff_site_code: str, card) -> None:
    """Index a pass just issued by its number, so a second card with it this cycle is caught too."""
    key = _card_key(cred, eff_site_code)
    if key:
        snapshot.ag_cards_by_site_card[key] = card


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
    should_stop: Callable[[], bool] | None = None,
    max_per_cycle: int = MAX_PROVISIONS_PER_CYCLE,
    require_unique_card: bool = False,
) -> int:
    provisioned = 0
    skipped = 0
    deduped = 0
    eligible = 0
    logger.info(
        "Phase 1: Checking for new credentials to provision (dedupe=%s)",
        "on" if dedupe_by_site_card else "off",
    )
    if not snapshot.ag_cards_read:
        # Every check below that a card is not issued twice reads the
        # listing; empty, they would all pass.
        logger.warning("Phase 1: AccessGrid's pass list was not read — issuing nothing this cycle")
        return 0

    for pid, creds in snapshot.credentials_by_person.items():
        person = snapshot.people.get(pid)
        if person is None or not person.active:
            continue
        if should_stop is not None and should_stop():
            logger.warning("Phase 1: configuration changed — stopping early")
            break
        for cred in creds:
            if cred.hold:
                continue
            if not cred.trigger_active:
                skipped += 1
                continue

            # Per-credential site code (Alta) wins over the global setting.
            eff_site_code = cred.site_code or site_code

            existing = tracking.get(pid, cred.id)
            if existing and existing.ag_card_id:
                # Already provisioned (or already deduped). Its AccessGrid
                # state is not recorded here: phases 2 and 4 tell which side
                # changed by comparing against the last state recorded, so
                # recording it first hid every AccessGrid-side suspend,
                # resume and unlink — phase 4 saw nothing to do and phase 2
                # pushed the PACS's old state back over it. Phase 4 records
                # the state once it has acted on it.
                continue
            if existing and existing.sync_error:
                # An issue that failed. Phase 5 retries it, up to its cap;
                # issuing it here too did so every cycle, with no cap at all.
                logger.debug(
                    "  %s %s: earlier issue failed (%s) — left to phase 5",
                    pid, cred.id, existing.sync_error,
                )
                continue

            if expired(cred):
                # Phase 3 deletes a pass whose card has expired, so issuing
                # one here would issue and delete a pass every cycle.
                # Extending the card's date in the PACS issues it again.
                logger.info(
                    "  %s %s: card expired on %s — skipping",
                    pid, cred.id, cred.deactivate_date.strftime("%Y-%m-%d"),
                )
                skipped += 1
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
            if require_unique_card and refuse_duplicate(snapshot, pid, person.full_name, cred, eff_site_code):
                skipped += 1
                continue
            if unrecorded_pass(snapshot, pid, person.full_name, cred):
                skipped += 1
                continue

            eligible += 1
            if provisioned >= max_per_cycle:
                # Deliberately after the tracking row would have been written
                # so nothing is half-recorded; these are simply retried next
                # cycle, by which time an operator has had a chance to look.
                continue

            params = provision_params(person, cred, template_id, eff_site_code, extra_metadata,
                                      use_file_data, card_title, card_classification, default_pass_days)
            sync_ref = params["metadata"]["sync_ref"]

            if dryrun.active():
                # Checked before the pending row is written, not just before
                # the API call: a dry run that leaves rows behind is not a
                # dry run, and phase 5 would then retry a provision that
                # never happened.
                dryrun.would(
                    "provision a pass for %s (%s), credential %s",
                    person.full_name, pid, cred.id,
                )
                continue

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
                last_synced_department=person.department,
            )

            snapshot.issue_attempted.add((pid, cred.id))
            try:
                logger.info("  Provisioning: %s (%s)", person.full_name, pid)
                card = ag.access_cards.provision(**params)
                ag_card_id = getattr(card, "id", None)
                ag_state = (getattr(card, "state", "active") or "active").lower()
                record_issue(person, cred, ag_card_id, ag_state, sync_ref)
                provisioned += 1
                logger.info("  Provisioned AG card %s for %s", ag_card_id, person.full_name)
                note_issued_card(snapshot, cred, eff_site_code, card)
                if cred.allocate_identity and pacs is not None:
                    # Hand the freshly-minted identity straight back so the
                    # card exists in the PACS before the next cycle reads it.
                    push_allocated_identities(pacs, pid, cred.id, card)
            except AccessGridError as e:
                logger.error("  Provision failed for %s: %s", person.full_name, e)
                tracking.record_error(pid, cred.id, str(e))
            except Exception as e:  # noqa: BLE001
                logger.error("  Unexpected error provisioning %s: %s", person.full_name, e, exc_info=True)
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


def provision_params(person, cred, template_id, eff_site_code, extra_metadata=None,
                     use_file_data=False, card_title="", card_classification="", default_pass_days=365):
    """The single payload builder for first issues and retries, pairs and singles."""
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
        "employee_id": person.id,
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
    return params


def record_issue(person, cred, card_id, state, sync_ref):
    """Keep the returned pair ID intact: mutations must never target a half."""
    tracking.upsert(
        pacs_person_id=person.id, pacs_credential_id=cred.id, ag_card_id=card_id,
        full_name=person.full_name, employee_id=person.id,
        status="active" if state in ("active", "created") else state,
        last_synced_email=person.email, last_synced_phone=person.phone,
        last_synced_full_name=person.full_name, last_synced_title=person.title,
        last_synced_department=person.department, last_known_ag_state=state, sync_ref=sync_ref,
    )

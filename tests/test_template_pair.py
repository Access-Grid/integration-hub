"""Issuing against a card template *pair*.

A pair returns a unified pass rather than a card: the credentials live in
`details`, one per platform, and the pass itself carries no card number.
Two things broke on that shape — nothing was written into the PACS, and the
pass could not be found again on later cycles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agsync.sync.phases.writeback import (
    identities_from_card,
    identities_from_cards,
    push_allocated_identities,
)
from agsync.sync.snapshot import Snapshot

EXPIRES = "2027-08-22T05:07:55.521Z"


def _pair():
    """The real response shape, from a live issue against a pair."""
    return SimpleNamespace(
        id="I_UgcwkCz7nO01s",
        install_url="https://my.nfckey.co/a/I_UgcwkCz7nO01s",
        state="created",
        status="success",
        details=[
            SimpleNamespace(
                id="ewXEpYZyG2Fimj4", card_template_id="81e8eaf527a",
                card_number="1216", site_code="2", file_data=None,
                expiration_date=EXPIRES, state="created",
            ),
            SimpleNamespace(
                id="h_6ue16ECOc8N2M", card_template_id="92b4fe98378",
                card_number="1217", site_code="2", file_data=None,
                expiration_date=EXPIRES, state="created",
            ),
        ],
    )


# --- reading the credentials out ----------------------------------------


def test_both_halves_of_a_pair_become_credentials():
    # This is what "exposes no credential yet — nothing to write back" was:
    # the pass has no card_number of its own, only its details do.
    identities = identities_from_card(_pair())
    assert [(i.site_code, i.card_number) for i in identities] == [
        ("2", "1216"), ("2", "1217"),
    ]


def test_each_half_keeps_its_own_expiry_as_a_datetime():
    """AccessGrid sends ISO strings and the SDK does not parse them.

    Passing one through as-is gives a CredentialIdentity whose annotation
    promises a datetime but holds a str, and the PACS adapter does date
    arithmetic on it: "'str' object has no attribute 'tzinfo'", which failed
    the whole writeback for that cardholder.
    """
    expected = datetime(2027, 8, 22, 5, 7, 55, 521000, tzinfo=UTC)
    for identity in identities_from_card(_pair()):
        assert identity.deactivate_date == expected


def test_dates_already_parsed_are_left_alone():
    when = datetime(2027, 1, 1, tzinfo=UTC)
    card = SimpleNamespace(
        id="x", site_code="66", card_number="5001", expiration_date=when, details=[],
    )
    assert identities_from_card(card)[0].deactivate_date == when


def test_an_unparseable_date_does_not_break_the_writeback():
    # Better a pass with default validity than no card in the PACS at all.
    card = SimpleNamespace(
        id="x", site_code="66", card_number="5001",
        expiration_date="whenever", details=[],
    )
    assert identities_from_card(card)[0].deactivate_date is None


def test_a_single_template_issue_still_works():
    card = SimpleNamespace(
        id="x", site_code="66", card_number="5001", expiration_date=None, details=[],
    )
    assert [(i.site_code, i.card_number) for i in identities_from_card(card)] == [
        ("66", "5001"),
    ]


def test_a_pass_with_nothing_allocated_yields_nothing():
    empty = SimpleNamespace(id="x", details=[], expiration_date=None)
    assert identities_from_card(empty) == []


def test_duplicate_halves_are_not_written_twice():
    same = _pair()
    same.details.append(same.details[0])
    assert len(identities_from_card(same)) == 2


def test_a_pair_reaches_the_pacs():
    class Receiving:
        supports_credential_writeback = True

        def __init__(self):
            self.written: list = []

        def write_back_credentials(self, person_id, credential_id, identities):
            self.written = identities
            return True

    pacs = Receiving()
    assert push_allocated_identities(pacs, "11587", "seos-slot1", _pair()) is True
    assert [(i.site_code, i.card_number) for i in pacs.written] == [
        ("2", "1216"), ("2", "1217"),
    ]


# --- finding the pass again ---------------------------------------------


def test_a_paired_pass_is_found_by_its_tag_not_its_id():
    """Listing a pair template returns the individual cards.

    The unified id we track is not among them, so an id lookup misses and
    every later phase would conclude the pass had been deleted. The cards do
    carry the employee id and the pacs_credential_id we stamped.
    """
    snap = Snapshot()
    half = SimpleNamespace(id="ewXEpYZyG2Fimj4", state="created")
    snap.ag_cards_by_token[("11587", "seos-slot1")] = [half]

    assert snap.resolve_ag_card("I_UgcwkCz7nO01s", "11587", "seos-slot1") is half


def test_the_id_is_preferred_when_it_is_present():
    snap = Snapshot()
    by_id = SimpleNamespace(id="card-1", state="created")
    by_token = SimpleNamespace(id="other", state="suspended")
    snap.ag_card_by_id["card-1"] = by_id
    snap.ag_cards_by_token[("p1", "slot1")] = [by_token]

    assert snap.resolve_ag_card("card-1", "p1", "slot1") is by_id


def test_a_genuinely_missing_pass_still_reads_as_missing():
    # The fallback must not paper over a real deletion, which is what
    # phase 3 exists to notice.
    assert Snapshot().resolve_ag_card("gone", "p1", "slot1") is None


# --- the sync reference --------------------------------------------------


def test_a_reference_finds_both_halves_of_a_pair():
    """The exact join, and the reason for stamping metadata at issue time.

    Every card one issue produced carries the same reference, so a pair
    resolves to both halves — where the unified id resolves to neither.
    """
    snap = Snapshot()
    first = SimpleNamespace(
        id="ewXEpYZyG2Fimj4", site_code="2", card_number="1216",
        metadata={"sync_ref": "abc123", "pacs_credential_id": "seos-slot1"},
        employee_id="11587", state="created", expiration_date=EXPIRES,
    )
    second = SimpleNamespace(
        id="h_6ue16ECOc8N2M", site_code="2", card_number="1217",
        metadata={"sync_ref": "abc123", "pacs_credential_id": "seos-slot1"},
        employee_id="11587", state="created", expiration_date=EXPIRES,
    )
    snap.ag_cards_by_sync_ref["abc123"] = [first, second]

    found = snap.resolve_ag_cards("I_UgcwkCz7nO01s", "11587", "seos-slot1", "abc123")
    assert found == [first, second]
    assert [(i.site_code, i.card_number) for i in identities_from_cards(found)] == [
        ("2", "1216"), ("2", "1217"),
    ]


def test_the_reference_beats_the_ambiguous_token():
    """Several cards can share (employee, credential) after re-issues.

    The token index keeps only the last one seen, so without a reference a
    tracked pass can resolve to an unrelated card from an earlier attempt.
    """
    snap = Snapshot()
    stale = SimpleNamespace(id="old", state="suspended")
    current = SimpleNamespace(id="new", state="created")
    snap.ag_cards_by_token[("11587", "seos-slot1")] = [stale]
    snap.ag_cards_by_sync_ref["abc123"] = [current]

    assert snap.resolve_ag_card("missing", "11587", "seos-slot1", "abc123") is current


def test_cards_issued_before_references_still_resolve():
    # Upgrading must not orphan passes already out in the world.
    snap = Snapshot()
    legacy = SimpleNamespace(id="legacy", state="created")
    snap.ag_cards_by_token[("11587", "seos-slot1")] = [legacy]

    assert snap.resolve_ag_card(None, "11587", "seos-slot1", "") is legacy


def test_an_unknown_reference_does_not_invent_a_card():
    assert Snapshot().resolve_ag_cards("x", "p", "c", "nope") == []


def test_identities_are_unioned_without_duplicates():
    half = SimpleNamespace(
        id="a", site_code="2", card_number="1216", expiration_date=EXPIRES, details=[],
    )
    assert len(identities_from_cards([half, half])) == 1


def test_a_single_pass_object_is_accepted_too():
    # Phase 1 hands over the provision response directly, not a list.
    assert len(identities_from_cards(_pair())) == 2


def test_a_legacy_pair_resolves_to_both_halves_without_a_reference():
    """Passes issued before references existed still have two halves.

    The token index used to keep one card per key, so such a pass resolved
    to half of itself and only one platform's card reached the PACS.
    """
    snap = Snapshot()
    apple = SimpleNamespace(
        id="ewXEpYZyG2Fimj4", site_code="2", card_number="1216",
        expiration_date=EXPIRES, details=[],
    )
    android = SimpleNamespace(
        id="h_6ue16ECOc8N2M", site_code="2", card_number="1217",
        expiration_date=EXPIRES, details=[],
    )
    snap.ag_cards_by_token[("11587", "seos-slot1")] = [apple, android]

    found = snap.resolve_ag_cards("I_UgcwkCz7nO01s", "11587", "seos-slot1", "")
    assert [(i.site_code, i.card_number) for i in identities_from_cards(found)] == [
        ("2", "1216"), ("2", "1217"),
    ]

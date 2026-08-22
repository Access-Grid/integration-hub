"""Issuing against a card template *pair*.

A pair returns a unified pass rather than a card: the credentials live in
`details`, one per platform, and the pass itself carries no card number.
Two things broke on that shape — nothing was written into the PACS, and the
pass could not be found again on later cycles.
"""

from __future__ import annotations

from types import SimpleNamespace

from agsync.sync.phases.writeback import identities_from_card, push_allocated_identities
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


def test_each_half_keeps_its_own_expiry():
    assert all(i.deactivate_date == EXPIRES for i in identities_from_card(_pair()))


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
    snap.ag_cards_by_token[("11587", "seos-slot1")] = half

    assert snap.resolve_ag_card("I_UgcwkCz7nO01s", "11587", "seos-slot1") is half


def test_the_id_is_preferred_when_it_is_present():
    snap = Snapshot()
    by_id = SimpleNamespace(id="card-1", state="created")
    by_token = SimpleNamespace(id="other", state="suspended")
    snap.ag_card_by_id["card-1"] = by_id
    snap.ag_cards_by_token[("p1", "slot1")] = by_token

    assert snap.resolve_ag_card("card-1", "p1", "slot1") is by_id


def test_a_genuinely_missing_pass_still_reads_as_missing():
    # The fallback must not paper over a real deletion, which is what
    # phase 3 exists to notice.
    assert Snapshot().resolve_ag_card("gone", "p1", "slot1") is None

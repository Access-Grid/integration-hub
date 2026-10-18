"""Only the cells that differ, and only when the plan still holds.

Two properties, and they are not the same property.

**Diff.** A write carries the cells whose value differs from a reading of
the PACS, and nothing else. Not the whole record — that is what the
cardholder save did, and why a field we failed to echo was a field we
erased. Not every cell we have an opinion about either, because Millennium's
own formatting differs from ours and rewriting a date that already says the
right thing is both pointless and a chance to get somebody's expiry wrong.

**Preconditions.** Diffing alone would still overwrite a card an operator
added after we planned: the plan says "put 90 in slot 3", the slot now holds
their 555, and a diff cheerfully reports that 555 is not 90. So an opinion
states what it assumed — *this slot was empty*, *this slot held card 555* —
and a slot whose assumption stopped being true is dropped rather than
written. The next cycle re-plans against what is actually there.
"""

from __future__ import annotations

from functools import partial

from agsync.lib.pacs.millennium_ultra import plan
from agsync.lib.pacs.millennium_ultra.adapter import parse_datetime
from agsync.lib.pacs.millennium_ultra.import_rows import to_csv

PARSE = partial(parse_datetime, offset_seconds=0)
EMPLOYEE = "22344"


def _plan(*opinions: plan.SlotOpinion, employee_id: str = EMPLOYEE) -> dict:
    cardholder = plan.CardholderPlan(employee_id=employee_id)
    for opinion in opinions:
        cardholder.want(opinion)
    return {"11666": cardholder}


def _snapshot(**slots: plan.SlotState) -> dict:
    return {"11666": {int(index.lstrip("s")): state for index, state in slots.items()}}


# =====================================================================
# Diff
# =====================================================================


def test_a_cell_that_already_matches_is_not_written():
    current = plan.SlotState(card_number="5001", active=False)
    wanted = plan.SlotOpinion(index=2, active=False, assumed_card="5001")

    assert plan.diff_slot(current, wanted, PARSE) is None


def test_only_the_differing_cell_is_written():
    """The opinion names a number and a state; only the state moved."""
    current = plan.SlotState(card_number="5001", facility_code="66", active=True)
    wanted = plan.SlotOpinion(
        index=2, card_number="5001", facility_code="66", active=False,
        assumed_card="5001",
    )

    change = plan.diff_slot(current, wanted, PARSE)

    assert change is not None
    assert change.active is False
    assert change.card_number is None, "unchanged, so absent"
    assert change.facility_code is None


def test_fields_we_have_no_opinion_about_are_never_compared():
    """A suspend has a view on one cell and no business with the rest.

    The slot's number, dates and format all differ from the defaults here,
    and none of them reaches the result.
    """
    current = plan.SlotState(
        card_number="5001", facility_code="66", card_format="HID 37",
        activation="01/01/2026 12:00 AM", expiration="01/01/2030 12:00 AM",
        active=True,
    )
    wanted = plan.SlotOpinion(index=2, active=False, assumed_card="5001")

    change = plan.diff_slot(current, wanted, PARSE)

    assert change is not None
    assert change.active is False
    for field in ("card_number", "facility_code", "card_format",
                  "activation", "expiration"):
        assert getattr(change, field) is None, field


def test_a_date_written_two_ways_is_the_same_date():
    """The export prints seconds where the form does not.

    `9/11/2026 12:00:00 AM` and `09/11/2026 12:00 AM` are one instant. Read
    as text they differ, which would rewrite every date on the install on
    every cycle — and each rewrite is a chance to get an expiry wrong.
    """
    current = plan.SlotState(
        card_number="5001", activation="9/11/2026 12:00:00 AM"
    )
    wanted = plan.SlotOpinion(
        index=2, activation="09/11/2026 12:00 AM", assumed_card="5001"
    )

    assert plan.diff_slot(current, wanted, PARSE) is None


def test_a_genuinely_different_date_is_written():
    current = plan.SlotState(
        card_number="5001", activation="9/11/2026 12:00:00 AM"
    )
    wanted = plan.SlotOpinion(
        index=2, activation="09/12/2026 12:00 AM", assumed_card="5001"
    )

    change = plan.diff_slot(current, wanted, PARSE)

    assert change is not None
    assert change.activation == "09/12/2026 12:00 AM"


def test_a_date_neither_side_can_parse_falls_back_to_the_text():
    """And therefore writes rather than assuming equality.

    The conservative direction: a value we cannot read is one we should not
    claim already says the right thing.
    """
    current = plan.SlotState(card_number="5001", activation="whenever")
    wanted = plan.SlotOpinion(
        index=2, activation="09/12/2026 12:00 AM", assumed_card="5001"
    )

    assert plan.diff_slot(current, wanted, PARSE) is not None


def test_formats_and_numbers_are_compared_without_case_or_padding():
    current = plan.SlotState(card_number=" 5001 ", card_format="HID 37")
    wanted = plan.SlotOpinion(
        index=2, card_number="5001", card_format="hid 37", assumed_card="5001"
    )

    assert plan.diff_slot(current, wanted, PARSE) is None


def test_a_cardholder_with_nothing_to_change_produces_no_row():
    diff = plan.build(
        _plan(plan.SlotOpinion(index=2, active=True, assumed_card="5001")),
        _snapshot(s2=plan.SlotState(card_number="5001", active=True)),
        PARSE,
    )

    assert diff.rows == []
    assert [d.reason for d in diff.unchanged] == ["slot 2 already says this"]
    assert diff.diverged == []


def test_the_file_carries_only_the_diffed_cells():
    diff = plan.build(
        _plan(
            plan.SlotOpinion(
                index=2, card_number="5001", facility_code="66", active=False,
                assumed_card="5001",
            )
        ),
        _snapshot(s2=plan.SlotState(card_number="5001", facility_code="66", active=True)),
        PARSE,
    )

    assert to_csv(diff.rows) == (
        "agsync_employee_id,agsync_card_2_active\r\n"
        "22344,False\r\n"
    )


# =====================================================================
# Preconditions — the race
# =====================================================================


def test_a_slot_filled_since_planning_is_left_alone():
    """The case this whole layer exists for.

    We read slot 3 as free and decided to write card 90 into it. An operator
    put their own card 555 there before we got to the import. A plain diff
    would see 555 != 90 and overwrite a working card.
    """
    diff = plan.build(
        _plan(
            plan.SlotOpinion(
                index=3, card_number="90", facility_code="99", assumed_empty=True
            )
        ),
        _snapshot(s3=plan.SlotState(card_number="555", facility_code="1")),
        PARSE,
    )

    assert diff.rows == []
    assert len(diff.diverged) == 1
    assert "somebody filled it in between" in diff.diverged[0].reason


def test_a_slot_that_is_still_free_is_written():
    diff = plan.build(
        _plan(
            plan.SlotOpinion(
                index=3, card_number="90", facility_code="99", assumed_empty=True
            )
        ),
        _snapshot(s3=plan.SlotState()),
        PARSE,
    )

    assert len(diff.rows) == 1
    assert diff.rows[0].slots[0].card_number == "90"
    assert diff.diverged == []


def test_a_card_swapped_since_planning_is_not_switched_off_in_its_place():
    """A suspend targets a card, not a position.

    Operators move cards between slots and replace them. Switching off
    whatever now sits where our card used to be is a revocation nobody asked
    for, applied to somebody else's credential.
    """
    diff = plan.build(
        _plan(plan.SlotOpinion(index=2, active=False, assumed_card="5001")),
        _snapshot(s2=plan.SlotState(card_number="7777", active=True)),
        PARSE,
    )

    assert diff.rows == []
    assert "the card moved or was replaced" in diff.diverged[0].reason


def test_one_diverged_slot_drops_the_whole_cardholder():
    """Nothing partial.

    Writing the half that still fits would leave the record in a state no
    phase asked for, and the plan is cheap to remake.
    """
    diff = plan.build(
        _plan(
            plan.SlotOpinion(index=2, active=False, assumed_card="5001"),
            plan.SlotOpinion(index=3, card_number="90", assumed_empty=True),
        ),
        _snapshot(
            s2=plan.SlotState(card_number="5001", active=True),
            s3=plan.SlotState(card_number="555"),
        ),
        PARSE,
    )

    assert len(diff.diverged) == 1
    # The row for slot 2 is built; the caller refuses on any divergence.
    assert len(diff.rows) == 1
    assert diff.rows[0].slots[0].index == 2


def test_a_cardholder_missing_from_the_reading_is_never_written_to():
    """Absence is not evidence that their slots are empty.

    It is also exactly what a renamed or deleted record looks like, and
    writing by name into that gap is how somebody else gets the card.
    """
    diff = plan.build(
        _plan(plan.SlotOpinion(index=2, active=False, assumed_card="5001")),
        {},
        PARSE,
    )

    assert diff.rows == []
    assert "not in the reading taken before this import" in diff.diverged[0].reason


def test_a_slot_the_reading_does_not_mention_counts_as_empty():
    """Which makes `assumed_card` fail closed and `assumed_empty` pass.

    A slot the export left out of its columns holds nothing, so a plan that
    wanted to fill it still may, and one that targeted a card in it may not.
    """
    filling = plan.build(
        _plan(plan.SlotOpinion(index=3, card_number="90", assumed_empty=True)),
        _snapshot(s1=plan.SlotState(card_number="1")),
        PARSE,
    )
    assert len(filling.rows) == 1

    targeting = plan.build(
        _plan(plan.SlotOpinion(index=3, active=False, assumed_card="5001")),
        _snapshot(s1=plan.SlotState(card_number="1")),
        PARSE,
    )
    assert targeting.rows == []
    assert "the card moved or was replaced" in targeting.diverged[0].reason


# =====================================================================
# Merging opinions
# =====================================================================


def test_two_phases_speaking_about_one_slot_merge_field_by_field():
    """Phase 1 writes a card, phase 4 holds it inactive. One row.

    The later opinion wins per field, so neither phase has to know the
    other ran.
    """
    cardholder = plan.CardholderPlan(employee_id=EMPLOYEE)
    cardholder.want(
        plan.SlotOpinion(
            index=3, card_number="90", facility_code="99", active=True,
            assumed_empty=True,
        ),
        "seos",
    )
    cardholder.want(plan.SlotOpinion(index=3, active=False), "seos")

    diff = plan.build({"11666": cardholder}, _snapshot(s3=plan.SlotState()), PARSE)

    change = diff.rows[0].slots[0]
    assert change.card_number == "90"
    assert change.active is False, "the later opinion wins"


def test_a_precondition_survives_a_later_opinion_that_omits_it():
    """Assumptions accumulate; they are not overwritten.

    A second speaker with no view on what the slot held must not be able to
    discard the first speaker's reason for choosing it.
    """
    cardholder = plan.CardholderPlan(employee_id=EMPLOYEE)
    cardholder.want(
        plan.SlotOpinion(index=3, card_number="90", assumed_empty=True)
    )
    cardholder.want(plan.SlotOpinion(index=3, active=False))

    diff = plan.build(
        {"11666": cardholder},
        _snapshot(s3=plan.SlotState(card_number="555")),
        PARSE,
    )

    assert diff.rows == []
    assert "somebody filled it in between" in diff.diverged[0].reason

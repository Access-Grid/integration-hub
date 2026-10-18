"""What to change about a cardholder, and the smallest file that says it.

This is the whole write path's thinking, with no I/O in it. The adapter
collects *opinions* about card slots while the phases run, and at the end of
the cycle this module turns them into import rows — carrying only the cells
that actually differ from what Millennium holds, and dropping any row whose
assumptions stopped being true.

Three ideas, in the order they matter.

**An opinion is partial.** A phase that suspends a card has an opinion about
one cell: ``Card 2 Active``. It has no opinion about that card's number, its
dates, its access levels, or the other two slots. Fields left ``None`` are
not our business, are never compared, and never reach the file. This is what
keeps the diff from churning: a true diff of the whole record would emit a
cell every time Millennium's own formatting differed from ours, forever.

**A diff is against a fresh reading.** The opinions are formed early in the
cycle, from pages read minutes ago. The export taken immediately before the
import is what they are diffed against, so a change an operator made in
between is already in the baseline — and a cell that now matches is simply
not written.

**An opinion carries what it assumed.** Diffing alone is not enough, and
this is the part worth being careful about. Suppose planning saw slot 3 empty
and decided to write card 90 into it. An operator fills slot 3 with their own
card 555 before the import. A plain diff asks "is 555 different from 90?",
answers yes, and overwrites a working card. So an opinion states its
precondition — *this slot was empty*, or *this slot held card 555* — and a
row whose precondition no longer holds is dropped rather than written. Next
cycle re-plans against the new state.

The dates deserve a note of their own. Millennium's export prints
``9/11/2026 12:00:00 AM`` where the form writes ``09/11/2026 12:00 AM``:
the same instant, different text. They are therefore compared as parsed
moments, never as strings, because a textual compare would rewrite every
date on every cycle and each rewrite is a chance to get one wrong.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from .import_rows import RowWrite, SlotWrite

logger = logging.getLogger(__name__)

# Access levels are deliberately not modelled here, and it is worth saying
# why rather than leaving a gap. The import does offer them — ten columns per
# card — but it labels them with an install-specific suffix
# ("Card 1 Access Level 1  [Global]") that is only readable off the page an
# upload answers with, so the column name cannot be known before the file is
# built. Nothing in this module can therefore express "give this card the
# levels its sibling has", and the adapter keeps the full-form save for the
# one write that needs it: a card going into an empty slot on an install that
# puts access levels on its trigger cards. On this install every slot reads
# "{}", so it does not arise.


@dataclass(frozen=True)
class SlotState:
    """What a card slot holds, as a reading of the PACS reports it.

    Every field is the text Millennium gave us, not a parsed value, because
    this is a baseline to compare against rather than something to reason
    with. ``active`` is the one exception: the export spells it True/False
    and an empty slot leaves it blank, which is a third thing.
    """

    card_number: str = ""
    facility_code: str = ""
    card_format: str = ""
    activation: str = ""
    expiration: str = ""
    active: bool | None = None

    @property
    def empty(self) -> bool:
        return not self.card_number.strip()


@dataclass(frozen=True)
class SlotOpinion:
    """What we want one slot to hold, and what we assumed when we decided.

    A field left ``None`` is one we have no opinion about. It is never
    compared and never written, which is the difference between this and a
    record we would post back.
    """

    index: int
    card_number: str | None = None
    facility_code: str | None = None
    card_format: str | None = None
    activation: str | None = None
    # Deliberately almost never set. Millennium owns the expiry: AccessGrid
    # applies a default of its own when issuing, and writing that back
    # stamped a date nobody chose onto the card — which phase 3 then treats
    # as grounds to delete the pass.
    expiration: str | None = None
    active: bool | None = None

    # -- preconditions --------------------------------------------------
    # What the slot looked like when this opinion was formed. Checked again
    # against a fresh reading before anything is written.

    # We chose this slot because nothing was in it.
    assumed_empty: bool = False
    # We targeted the card with this number. Set for anything that acts on
    # an existing card, so a suspend lands on the card we meant rather than
    # on whatever now occupies its position.
    assumed_card: str | None = None

    def wanted(self) -> dict[str, object]:
        """The fields this opinion actually has a view on."""
        return {
            name: value
            for name, value in (
                ("card_number", self.card_number),
                ("facility_code", self.facility_code),
                ("card_format", self.card_format),
                ("activation", self.activation),
                ("expiration", self.expiration),
                ("active", self.active),
            )
            if value is not None
        }


@dataclass
class CardholderPlan:
    """Everything this cycle wants changed about one cardholder.

    Addressed by Employee ID, which is what the import matches a row on and
    which is unique across cardholders on this install. A blank one cannot
    be written to at all, so the caller establishes that it is present
    before building a plan at all.
    """

    employee_id: str
    # Which credential asked for each slot, so a confirmed write can be
    # recorded against the right ledger entry.
    credential_ids: dict[int, str] = field(default_factory=dict)
    slots: dict[int, SlotOpinion] = field(default_factory=dict)

    def want(self, opinion: SlotOpinion, credential_id: str = "") -> None:
        """Add or merge an opinion about one slot.

        Two phases can speak about the same slot in one cycle — a credential
        written in phase 1 and held inactive in phase 4 is the ordinary
        case — and the later opinion wins field by field. Preconditions
        accumulate rather than being replaced: an assumption made by either
        speaker still has to hold.
        """
        existing = self.slots.get(opinion.index)
        if existing is None:
            self.slots[opinion.index] = opinion
        else:
            merged = {
                name: value
                for name, value in opinion.wanted().items()
            }
            self.slots[opinion.index] = replace(
                existing,
                assumed_empty=existing.assumed_empty or opinion.assumed_empty,
                assumed_card=existing.assumed_card or opinion.assumed_card,
                **merged,  # type: ignore[arg-type]
            )
        if credential_id:
            self.credential_ids[opinion.index] = credential_id


@dataclass(frozen=True)
class Dropped:
    """A row that will not be written, and why."""

    person_id: str
    slot: int
    reason: str


@dataclass(frozen=True)
class Diff:
    """The outcome of planning: what to send, and what was left out."""

    rows: list[RowWrite]
    # (person_id, slot) for every cell that is going to be written, so the
    # caller can record exactly what it asked for and nothing more.
    written: dict[str, dict[int, SlotWrite]]
    unchanged: list[Dropped]
    diverged: list[Dropped]


def _texts_match(ours: str, theirs: str) -> bool:
    return ours.strip().casefold() == theirs.strip().casefold()


def _moments_match(ours: str, theirs: str, parse) -> bool:
    """Do two of Millennium's date strings mean the same instant?

    The export prints seconds and no leading zero where the form does the
    opposite, so the texts differ for every date on the install. Compared as
    text this rewrites every date every cycle; compared as moments it is the
    no-op it should be. A string neither side can parse falls back to a text
    compare, which is the conservative answer — it writes rather than
    assuming equality.
    """
    mine, yours = parse(ours), parse(theirs)
    if mine is None or yours is None:
        return _texts_match(ours, theirs)
    return mine == yours


def diff_slot(
    current: SlotState, wanted: SlotOpinion, parse_datetime
) -> SlotWrite | None:
    """The cells that differ, or None when there is nothing to say.

    Only fields the opinion has a view on are considered. Everything else
    about the slot — and the whole rest of the cardholder — is absent from
    the result and therefore absent from the file.
    """
    cells: dict[str, object] = {}

    if wanted.card_number is not None and not _texts_match(
        wanted.card_number, current.card_number
    ):
        cells["card_number"] = wanted.card_number
    if wanted.facility_code is not None and not _texts_match(
        wanted.facility_code, current.facility_code
    ):
        cells["facility_code"] = wanted.facility_code
    if wanted.card_format is not None and not _texts_match(
        wanted.card_format, current.card_format
    ):
        cells["card_format"] = wanted.card_format
    if wanted.activation is not None and not _moments_match(
        wanted.activation, current.activation, parse_datetime
    ):
        cells["activation"] = wanted.activation
    if wanted.expiration is not None and not _moments_match(
        wanted.expiration, current.expiration, parse_datetime
    ):
        cells["expiration"] = wanted.expiration
    if wanted.active is not None and wanted.active != current.active:
        cells["active"] = wanted.active

    if not cells:
        return None
    return SlotWrite(index=wanted.index, **cells)  # type: ignore[arg-type]


def _precondition_failure(current: SlotState, wanted: SlotOpinion) -> str:
    """Why this slot must not be written, or "" when it may be.

    The guard that makes a stale plan safe rather than merely narrow. Both
    checks describe what the slot looked like when the decision was made,
    and both are re-asked against a reading taken as late as possible.
    """
    if wanted.assumed_empty and not current.empty:
        return (
            f"slot {wanted.index} was free when this cycle planned the write and "
            f"now holds card {current.card_number!r} — somebody filled it in "
            "between, so it is left alone"
        )
    if (
        wanted.assumed_card is not None
        and not _texts_match(wanted.assumed_card, current.card_number)
    ):
        return (
            f"slot {wanted.index} held card {wanted.assumed_card!r} when this "
            f"cycle planned the write and now holds {current.card_number!r} — "
            "the card moved or was replaced, so it is left alone"
        )
    return ""


def build(
    plans: dict[str, CardholderPlan],
    snapshot: dict[str, dict[int, SlotState]],
    parse_datetime,
) -> Diff:
    """Turn a cycle's opinions into the rows an import should carry.

    `snapshot` is a reading taken as close to the write as possible — on this
    adapter, a second bulk export fetched at flush time. A cardholder missing
    from it is dropped: absence is not evidence that their slots are empty,
    and it is exactly what a renamed or deleted record looks like.
    """
    rows: list[RowWrite] = []
    written: dict[str, dict[int, SlotWrite]] = {}
    unchanged: list[Dropped] = []
    diverged: list[Dropped] = []

    for person_id in sorted(plans):
        plan = plans[person_id]
        current_slots = snapshot.get(person_id)
        if current_slots is None:
            diverged.extend(
                Dropped(
                    person_id, index,
                    "the cardholder is not in the reading taken before this "
                    "import — renamed, deleted, or never matched — so nothing "
                    "is written to them",
                )
                for index in sorted(plan.slots)
            )
            continue

        changes: list[SlotWrite] = []
        for index in sorted(plan.slots):
            wanted = plan.slots[index]
            current = current_slots.get(index, SlotState())

            failure = _precondition_failure(current, wanted)
            if failure:
                diverged.append(Dropped(person_id, index, failure))
                continue

            change = diff_slot(current, wanted, parse_datetime)
            if change is None:
                unchanged.append(
                    Dropped(person_id, index, f"slot {index} already says this")
                )
                continue
            changes.append(change)

        if any(d.person_id == person_id for d in diverged):
            # Nothing partial, for anybody. One slot's assumptions having
            # moved says the page is not what this plan was made against, and
            # writing the half that still fits would leave the record in a
            # state no phase asked for. The whole cardholder waits a cycle;
            # everybody else in the same import is unaffected.
            continue
        if not changes:
            continue
        rows.append(RowWrite(employee_id=plan.employee_id, slots=tuple(changes)))
        written[person_id] = {change.index: change for change in changes}

    return Diff(
        rows=rows, written=written, unchanged=unchanged, diverged=diverged
    )

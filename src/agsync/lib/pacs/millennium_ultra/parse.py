"""Millennium Ultra HTML/JSON parsing — pure functions, no network.

Everything here operates on strings the client fetched, so the whole
mapping layer is testable against saved fixtures.

Two things drive the design:

  * **Parse by `name=`, never by visible label.** Millennium Ultra lets each
    tenant relabel fields. On the tenant we reverse-engineered, `MiddleName`
    is displayed as "Card Type" and holds HID/RFID, and the user fields are
    labelled "Unit Number" / "Resident Type" / "test". Only the form's
    `name` attributes are stable.

  * **Read the whole form, not the fields we care about.** Status writeback
    is a read-modify-write that must re-post every field unchanged; a field
    we failed to parse would be silently blanked in a production access
    control system. `parse_form` therefore collects *all* inputs, selects and
    textareas, and `serialize_form` replays them. Adding a field we don't
    understand costs nothing.

The vendor spells it `FaciltyCode` (sic) — see CARD_FIELD_FACILITY.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

# Cardholders have a fixed three card slots (page constant
# `cardsPerCardholder = 3`). Slot indexes are 1-based.
CARD_SLOTS = (1, 2, 3)

CARD_FIELD_ID = "Card_{n}_CardID"
CARD_FIELD_ENCODED = "Card_{n}_EncodedCardNumber"
CARD_FIELD_FACILITY = "Card_{n}_FaciltyCode"  # vendor's spelling, not a typo here
CARD_FIELD_FORMAT = "Card_{n}_CardFormat"
CARD_FIELD_ACTIVE = "Card_{n}_Active"
CARD_FIELD_ACTIVATION = "Card_{n}_ActivationDate"
CARD_FIELD_EXPIRATION = "Card_{n}_ExpirationDate"

# Roster display names arrive as "Last, TYPE. First" — e.g.
# "Abayan, HID. Tanyabella". We never build a person's name from this
# (FirstName/LastName on the detail page are authoritative); it is only
# used for logging and for the quick-search style listing.
ROSTER_NAME_RE = re.compile(r"^(?P<last>[^,]+),\s*(?:(?P<type>[^.]+)\.\s*)?(?P<first>.*)$")

# Letters the roster is bucketed by: A-Z plus 0 for "Other".
ROSTER_BUCKETS: tuple[int, ...] = tuple(range(65, 91)) + (0,)


# ---------------------------------------------------------------------------
# Generic form scraping
# ---------------------------------------------------------------------------


@dataclass
class Form:
    """Every named control on a page, in document order.

    `inputs` keeps duplicates (the page has several forms, and a few names
    repeat) so serialization can faithfully reproduce what the browser sends.
    """

    inputs: list[dict[str, Any]] = field(default_factory=list)
    selects: dict[str, list[tuple[str, str, bool]]] = field(default_factory=dict)
    textareas: dict[str, str] = field(default_factory=dict)

    def value(self, name: str, default: str = "") -> str:
        """First non-empty value for `name`, else the first value, else default."""
        seen = False
        for i in self.inputs:
            if i["name"] == name:
                seen = True
                if i["value"]:
                    return i["value"]
        if name in self.textareas:
            return self.textareas[name]
        return "" if seen else default

    def checked(self, name: str) -> bool:
        return any(i["name"] == name and i["checked"] for i in self.inputs)

    def present(self, name: str) -> bool:
        return (
            any(i["name"] == name for i in self.inputs)
            or name in self.selects
            or name in self.textareas
        )

    def selected(self, name: str) -> str:
        """Value of the selected <option>, or "" when nothing is selected.

        Millennium Ultra omits `selected` entirely on empty card slots, which
        is one of the signals that the slot is unused.
        """
        for value, _label, is_selected in self.selects.get(name, []):
            if is_selected:
                return value
        return ""

    def options(self, name: str) -> list[tuple[str, str]]:
        return [(v, label) for v, label, _sel in self.selects.get(name, [])]


class _FormScraper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.form = Form()
        self._select: str | None = None
        self._option: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "input":
            name = a.get("name")
            if name:
                self.form.inputs.append({
                    "name": name,
                    "type": (a.get("type") or "text").lower(),
                    "value": a.get("value", ""),
                    # Boolean attributes render bare (checked) or as
                    # checked="checked"; presence is what counts.
                    "checked": "checked" in a,
                    "disabled": "disabled" in a,
                })
        elif tag == "select":
            self._select = a.get("name")
            if self._select:
                self.form.selects.setdefault(self._select, [])
        elif tag == "option" and self._select:
            self._option = {
                "value": a.get("value", ""),
                "selected": "selected" in a,
                "text": "",
            }
        elif tag == "textarea":
            name = a.get("name")
            if name:
                self._textarea = name
                self.form.textareas.setdefault(name, "")

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option["text"] += data
        elif getattr(self, "_textarea", None):
            self.form.textareas[self._textarea] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._option is not None and self._select:
            self.form.selects[self._select].append(
                (self._option["value"], self._option["text"].strip(), self._option["selected"])
            )
            self._option = None
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            name = getattr(self, "_textarea", None)
            if name:
                self.form.textareas[name] = self.form.textareas[name].strip()
            self._textarea = None


def parse_form(html: str) -> Form:
    scraper = _FormScraper()
    scraper.feed(html)
    scraper.close()
    return scraper.form


def serialize_form(form: Form, overrides: dict[str, str | None] | None = None) -> list[tuple[str, str]]:
    """Reproduce what the browser would submit, with targeted overrides.

    Mirrors browser semantics: checkboxes and radios contribute only when
    checked, everything else always contributes. An override value of None
    removes the field (used to *uncheck* a checkbox); any other value replaces
    or adds it.

    Note we deliberately ignore the `disabled` attribute. Several inputs on
    the cardholder page render disabled yet still appear in the payload a real
    browser sends, because the page's JavaScript re-enables them before submit.
    Mirroring the captured payload matters more than mirroring the markup.
    """
    overrides = overrides or {}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    for item in form.inputs:
        name = item["name"]
        if item["type"] in ("submit", "button", "image", "file"):
            continue
        if name in overrides:
            if name not in seen:
                value = overrides[name]
                if value is not None:
                    out.append((name, value))
                seen.add(name)
            continue
        if item["type"] in ("checkbox", "radio") and not item["checked"]:
            continue
        out.append((name, item["value"]))
        seen.add(name)

    for name in form.selects:
        if name in overrides:
            if name not in seen:
                value = overrides[name]
                if value is not None:
                    out.append((name, value))
                seen.add(name)
            continue
        out.append((name, form.selected(name)))
        seen.add(name)

    for name, value in form.textareas.items():
        if name in overrides:
            if name not in seen:
                v = overrides[name]
                if v is not None:
                    out.append((name, v))
                seen.add(name)
            continue
        out.append((name, value))
        seen.add(name)

    # Overrides that add a field the page didn't have (e.g. checking a box
    # that renders unchecked, which emits no input value at all).
    for name, value in overrides.items():
        if name not in seen and value is not None:
            out.append((name, value))

    return out


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_DATE_FORMATS = ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y")


def parse_datetime(raw: str | None) -> datetime | None:
    """Parse Millennium Ultra's `MM/DD/YYYY hh:mm AM` timestamps.

    The app emits a malformed `00:00 AM` for midnight, which `%I` rejects
    (12-hour clocks have no hour zero). Normalize that to `12:00 AM` before
    parsing rather than dropping the date — an unparsed expiration would look
    like "no expiry" and silently extend a credential.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    text = re.sub(r"\b00:(\d{2})\s*(AM|PM)\b", r"12:\1 \2", text, flags=re.IGNORECASE)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardSlot:
    index: int
    card_id: str
    encoded: str
    facility: str
    card_format: str
    active: bool
    activation: datetime | None
    expiration: datetime | None

    @property
    def is_empty(self) -> bool:
        """An unused slot carries no card number.

        Empty slots render their inputs with no `value=` attribute at all, so
        both the encoded number and the card id come back blank.
        """
        return not self.encoded and not self.card_id

    @property
    def identity(self) -> str:
        """Stable id for the physical card, independent of its slot.

        Slot index and `CardID` are both reusable — delete a card and the next
        one can take its place — so keying on either would churn the
        AccessGrid pass. The card's own facility + number is durable.
        """
        if self.encoded:
            return f"{self.facility or '0'}:{self.encoded}"
        return f"slot:{self.index}:{self.card_id}"


def parse_card_slot(form: Form, n: int) -> CardSlot:
    return CardSlot(
        index=n,
        card_id=form.value(CARD_FIELD_ID.format(n=n)),
        encoded=form.value(CARD_FIELD_ENCODED.format(n=n)).strip(),
        facility=form.value(CARD_FIELD_FACILITY.format(n=n)).strip(),
        card_format=form.selected(CARD_FIELD_FORMAT.format(n=n)),
        active=form.checked(CARD_FIELD_ACTIVE.format(n=n)),
        activation=parse_datetime(form.value(CARD_FIELD_ACTIVATION.format(n=n))),
        expiration=parse_datetime(form.value(CARD_FIELD_EXPIRATION.format(n=n))),
    )


def parse_card_formats(html_or_form: str | Form) -> list[tuple[str, str]]:
    """Discover the tenant's card formats as [(id, label)].

    Format ids are per-tenant, so they are read from a live cardholder page at
    config time rather than hardcoded. Any slot's dropdown carries the full
    list; slot 1 always exists.
    """
    form = html_or_form if isinstance(html_or_form, Form) else parse_form(html_or_form)
    for n in CARD_SLOTS:
        options = form.options(CARD_FIELD_FORMAT.format(n=n))
        if options:
            return [(value, label) for value, label in options if value]
    return []


# ---------------------------------------------------------------------------
# Cardholders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cardholder:
    id: str
    first_name: str
    last_name: str
    email: str
    phone: str
    employee_id: str
    cards: list[CardSlot]
    form: Form | None = None

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)


def parse_cardholder(html: str, cardholder_id: str = "") -> Cardholder:
    form = parse_form(html)
    return Cardholder(
        id=form.value("ID") or cardholder_id,
        first_name=form.value("FirstName").strip(),
        last_name=form.value("LastName").strip(),
        # Present on the page but empty on tenants without the Personal
        # Information module; the adapter synthesizes an address when blank.
        email=form.value("EMail").strip(),
        phone=form.value("Phone").strip(),
        employee_id=form.value("EmployeeID").strip(),
        cards=[parse_card_slot(form, n) for n in CARD_SLOTS],
        form=form,
    )


def split_roster_name(name: str) -> tuple[str, str, str]:
    """"Abayan, HID. Tanyabella" -> ("Abayan", "HID", "Tanyabella")."""
    m = ROSTER_NAME_RE.match((name or "").strip())
    if not m:
        return "", "", (name or "").strip()
    return (
        (m.group("last") or "").strip(),
        (m.group("type") or "").strip(),
        (m.group("first") or "").strip(),
    )


# ---------------------------------------------------------------------------
# Email synthesis
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _NON_ALNUM.sub("", (text or "").lower())


def synth_email(first: str, last: str, domain: str, cardholder_id: str = "") -> str:
    """Build a delivery address for a cardholder that has none.

    No cardholder on the reference tenant has an email or phone, and phase 1
    skips anyone with neither, so without this nothing would ever provision.

    `cardholder_id` disambiguates collisions, which are guaranteed: the
    reference roster holds four separate records named "Acebedo, RFID. Jose".
    Callers pass it only for the second and later holders of a slug so the
    common case stays readable.
    """
    domain = (domain or "").strip().lstrip("@")
    local = f"{_slug(first)}{_slug(last)}"
    if not local or not domain:
        return ""
    if cardholder_id:
        local = f"{local}.{_slug(cardholder_id)}"
    return f"{local}@{domain}"


def assign_emails(people: list[tuple[str, str, str]], domain: str) -> dict[str, str]:
    """Map cardholder_id -> address for a whole roster, resolving collisions.

    `people` is [(cardholder_id, first, last)] in roster order. The first
    holder of a slug keeps the clean address; later ones get their id
    appended, so an address never silently moves between people as the roster
    grows.
    """
    counts: dict[str, int] = {}
    for _pid, first, last in people:
        slug = f"{_slug(first)}{_slug(last)}"
        counts[slug] = counts.get(slug, 0) + 1

    used: set[str] = set()
    out: dict[str, str] = {}
    for pid, first, last in people:
        slug = f"{_slug(first)}{_slug(last)}"
        if counts.get(slug, 0) > 1 or slug in used:
            out[pid] = synth_email(first, last, domain, cardholder_id=pid)
        else:
            out[pid] = synth_email(first, last, domain)
        used.add(slug)
    return out

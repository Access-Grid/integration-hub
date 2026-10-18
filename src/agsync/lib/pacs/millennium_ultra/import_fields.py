"""Millennium's import field catalogue, read off the page that offers it.

Step one of an import uploads the file to
``/DatabaseFunctions/ImportCardholders/SelectFields``, and the page that
comes back is the only place two things are stated:

  * **the system fields this install has**, each with the numeric ``ID``,
    ``Card`` slot and ``TenantID`` that step two's payload must name. They
    arrive as ``<a>`` elements carrying ``data-id``, ``data-card`` and
    ``data-tenant-id``, labelled the way the field screen spells them.
  * **which of our columns it read**, each with the ``data-user-column-id``
    step two has to echo back.

Reading the catalogue rather than hard-coding it is the point. The numeric
ids are install configuration — access levels run from 200, their activation
dates from 456, their expirations from 712 — and a hard-coded map is how
``FaciltyCode`` cost us a day: the name was wrong, nothing checked it, and
the write silently did nothing. Here a label we cannot find is raised before
a single row is sent.

Two things about this page were learned the hard way, by testing against the
live install, and both are load-bearing.

**A field entry of ``ID`` 1 with an empty ``Name`` means "this column is
deliberately unmapped".** It is not a sentinel the identity columns are
paired against, which is what an earlier reading of a capture suggested. The
real person-level fields are First Name 1, Last Name 2, Card Type 3 and
Employee ID 4, and the empty right-hand entries in ``#configured_fields``
are placeholders for columns nobody has assigned yet.

**A header that exactly matches a system field is placed automatically — and
that field is then removed from this page's own catalogue.** So naming a
column after the field it feeds is precisely how we lose the ability to read
back which numeric field it means. :mod:`import_rows` therefore names every
column with an alias that matches nothing, which keeps every column loose and
the catalogue whole. If one of our aliases is ever placed, that is a bug in
the aliasing and it is raised rather than worked around.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# The marker for a column we are carrying but not writing. Not used by this
# adapter — every column we send is mapped — but recognised so that a page
# carrying them is read correctly.
UNMAPPED_FIELD_ID = 1

# `#available_fields` holds the columns the server could not place,
# `#configured_fields` the ones it could, and `#selected_fields` the
# catalogue of system fields still unassigned. Only the list ids tell them
# apart; the markup inside is identical.
_AVAILABLE = "available_fields"
_CONFIGURED = "configured_fields"
_CATALOGUE = "selected_fields"


@dataclass(frozen=True)
class SystemField:
    """One importable field, as step two's payload needs it named."""

    label: str
    field_id: int
    card: int
    tenant_id: int

    def mapped_to(self, column_index: int, header: str) -> dict:
        """The field entry pointing `header` at this field.

        ``Name`` is Millennium's own label and must match it exactly — the
        access level labels carry a double space, for instance — which is
        why it is always read off the page and never typed here.
        """
        return {
            "Card": self.card,
            "ID": self.field_id,
            "Name": self.label,
            "TenantID": self.tenant_id,
            "UserColumnID": column_index,
            "UserColumnName": header,
        }


class ImportFieldsUnavailable(RuntimeError):
    """The field screen did not say what we need it to say.

    Raised rather than worked around. Every path out of here ends in a write
    to somebody's access control, and a guess about which numeric field means
    "facility code" is not a thing to write on.
    """


def _attr(attrs: list[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key == name:
            return value or ""
    return ""


class _FieldsParser(HTMLParser):
    """Pulls the three lists out of the field screen.

    Nesting is tracked by depth rather than tag name: the lists sit inside
    several layers of panels, and the only dependable marker is the ``id`` on
    the ``<div class="list-group">`` itself.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.catalogue: list[SystemField] = []
        # (column index, header) for every column of ours the server read.
        self.columns: list[tuple[int, str]] = []
        # Headers the server placed against a system field by itself.
        self.placed: list[str] = []
        self._list = ""
        self._depth = 0
        self._item: dict | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            if self._list:
                self._depth += 1
            else:
                name = _attr(attrs, "id")
                if name in (_AVAILABLE, _CONFIGURED, _CATALOGUE):
                    self._list = name
                    self._depth = 0
            return
        if tag != "a" or not self._list:
            return
        self._item = {
            "user_column_id": _attr(attrs, "data-user-column-id"),
            "left": _attr(attrs, "data-left"),
            "id": _attr(attrs, "data-id"),
            "card": _attr(attrs, "data-card"),
            "tenant_id": _attr(attrs, "data-tenant-id"),
        }
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._item is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._list:
            if self._depth == 0:
                self._list = ""
            else:
                self._depth -= 1
            return
        if tag != "a" or self._item is None:
            return
        item, self._item = self._item, None
        label = " ".join("".join(self._text).split())
        self._text = []

        if self._list == _CATALOGUE:
            if label and item["id"]:
                field = self._system_field(item, label)
                if field is not None:
                    self.catalogue.append(field)
            return

        # One of our columns, in whichever list names it.
        if item["user_column_id"] and (self._list == _AVAILABLE or item["left"]):
            try:
                self.columns.append((int(item["user_column_id"]), label))
            except ValueError:
                logger.debug(
                    "Millennium: unreadable column id %r", item["user_column_id"]
                )
            else:
                if self._list == _CONFIGURED:
                    self.placed.append(label)

    @staticmethod
    def _system_field(item: dict, label: str) -> SystemField | None:
        try:
            return SystemField(
                label=label,
                field_id=int(item["id"]),
                card=int(item["card"] or 0),
                tenant_id=int(item["tenant_id"] or 0),
            )
        except ValueError:
            logger.debug("Millennium: unreadable system field %r", label)
            return None


@dataclass(frozen=True)
class SelectFieldsPage:
    """What the server understood about the file we just uploaded."""

    catalogue: dict[str, SystemField]
    columns: dict[str, int]
    placed: tuple[str, ...]

    @classmethod
    def parse(cls, html: str) -> SelectFieldsPage:
        parser = _FieldsParser()
        parser.feed(html or "")
        page = cls(
            catalogue={f.label: f for f in parser.catalogue},
            columns={header: index for index, header in parser.columns},
            placed=tuple(parser.placed),
        )
        if not page.catalogue:
            raise ImportFieldsUnavailable(
                "Millennium's import page listed no system fields — it is not "
                "the field-selection screen, or the page shape has changed"
            )
        if not page.columns:
            raise ImportFieldsUnavailable(
                "Millennium's import page listed none of our file's columns — "
                "the upload did not arrive as a file with a header row"
            )
        return page

    def mapping(self, columns: list[tuple[str, str]]) -> list[dict]:
        """The field entries for step two, one per column of ours.

        `columns` is (header alias, Millennium label) in file order. Every
        column must be mapped — we only ever send columns we are writing — and
        the column index comes from the server's own
        ``data-user-column-id``, never from our position in the header row.
        The two agree on every capture we have; if they ever stop agreeing it
        is the server's number that decides which column it reads, so taking
        ours would write the right value into the wrong field.
        """
        aliases = [alias for alias, _ in columns]

        unread = [a for a in aliases if a not in self.columns]
        if unread:
            raise ImportFieldsUnavailable(
                "Millennium's import page did not read these columns: "
                f"{', '.join(unread)} — it read {', '.join(sorted(self.columns))}"
            )

        # Our aliases are built not to match any Millennium label, so the
        # server should never place one. If it has, the alias collided and
        # the field has been taken out of the catalogue — which is exactly
        # the condition that makes a mapping unreadable.
        collided = [a for a in aliases if a in self.placed]
        if collided:
            raise ImportFieldsUnavailable(
                f"Millennium placed our own column(s) {', '.join(collided)} "
                "against a field of its own, so their ids cannot be read back. "
                "The header aliasing needs to change"
            )

        missing = [label for _, label in columns if label not in self.catalogue]
        if missing:
            raise ImportFieldsUnavailable(
                "Millennium's import offers no field called "
                f"{', '.join(repr(m) for m in missing)} on this install — the "
                "field labels have changed and nothing would be written"
            )

        return [
            self.catalogue[label].mapped_to(self.columns[alias], alias)
            for alias, label in columns
        ]

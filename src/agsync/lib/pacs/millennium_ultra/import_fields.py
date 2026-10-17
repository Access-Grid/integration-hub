"""Millennium's import field catalogue, read off the page that offers it.

The import is a four-step conversation, and this module owns the awkward
middle. Step one uploads the CSV to
``/DatabaseFunctions/ImportCardholders/SelectFields``, and the page that
comes back is the only place two things are stated:

  * **the system fields this install has**, each with the numeric ``ID``,
    ``Card`` slot and ``TenantID`` that step two's JSON has to name. They
    arrive as ``<a>`` elements carrying ``data-id``, ``data-card`` and
    ``data-tenant-id``, labelled with the same text the export prints as its
    column headers — "Card 1 Encoded Card No.", "Card 2 Facility Code".
  * **which of our CSV's columns it actually read**, each with the
    ``data-user-column-id`` step two has to echo back.

Reading the catalogue rather than hard-coding it is the whole point. The
numeric ids are install configuration: access levels run from 200, their
activation dates from 456 and their expirations from 712, so an install with
a different number of them presents a different list. More to the point, a
hard-coded map is how ``FaciltyCode`` cost us a day — the name was wrong,
nothing checked it, and the write silently did nothing. Here a label we
cannot find is raised before a single row is sent.

**The page sorts our columns into two lists, and both matter.** A header
whose text exactly matches a system field is paired up for us and moves into
``#configured_fields``, as two ``<a>`` elements: the left one carrying our
column, the right one carrying that field's ids but rendered with no label at
all. A header that matches nothing is left loose in ``#available_fields``,
and the field it ought to go to stays in the ``#selected_fields`` catalogue.
Since we name our columns with the system labels, we mostly get the first
case — and reading only the catalogue would then find nothing, because the
server had already taken those entries out of it. So both are parsed, and a
pairing the server has already made is preferred to one we would infer.

Three columns are special. "First Name", "Last Name" and "Card Type" are not
system fields: the server pairs all three against the sentinel ``ID`` 1 with
an empty ``Name``, and uses them to decide which cardholder a row is about.
They are mandatory, they come first, and :mod:`import_rows` writes them back
exactly as the export printed them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# The identity columns, in the order the server pairs them. Not system
# fields: all three map to IDENTITY_FIELD_ID with an empty Name, and they are
# how the import matches a row to a cardholder.
IDENTITY_COLUMNS = ("First Name", "Last Name", "Card Type")

# The sentinel the server pairs the identity columns against.
IDENTITY_FIELD_ID = 1

# `#available_fields` holds the CSV columns the server could not place,
# `#configured_fields` the pairs it could, and `#selected_fields` the
# catalogue of system fields still unused. Only the list ids tell them
# apart — the markup inside is otherwise identical.
_AVAILABLE = "available_fields"
_CONFIGURED = "configured_fields"
_CATALOGUE = "selected_fields"


@dataclass(frozen=True)
class SystemField:
    """One importable field, as step two's JSON needs it named."""

    label: str
    field_id: int
    card: int
    tenant_id: int

    def mapped_to(self, column_index: int, header: str) -> dict:
        """The `ImportedFields` entry pointing `header` at this field.

        ``Name`` is the system field's label and ``UserColumnName`` our CSV
        header. They are the same text whenever we have named a column after
        the field it feeds, which is the convention :mod:`import_rows`
        follows — but a field the server paired up for us arrives with no
        label rendered, so the header stands in for it.
        """
        return {
            "Card": self.card,
            "ID": self.field_id,
            "Name": self.label or header,
            "TenantID": self.tenant_id,
            "UserColumnID": column_index,
            "UserColumnName": header,
        }


class ImportFieldsUnavailable(RuntimeError):
    """The SelectFields page did not say what we need it to say.

    Raised rather than worked around. Every path out of here ends in a write
    to somebody's access control, and a guess about which numeric field id
    means "facility code" is not a thing to write on.
    """


def _attr(attrs: list[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key == name:
            return value or ""
    return ""


class _FieldsParser(HTMLParser):
    """Pulls the three field lists out of the SelectFields page.

    Nesting is tracked by depth rather than by tag name: the lists sit inside
    several layers of Bootstrap panels, and the only dependable marker is the
    ``id`` on the ``<div class="list-group">`` itself.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # label -> field, for the fields still unassigned.
        self.catalogue: list[SystemField] = []
        # (column_index, header) for every CSV column the server read.
        self.columns: list[tuple[int, str]] = []
        # (header, field) for the pairs the server made itself.
        self.paired: list[tuple[str, SystemField]] = []
        self._list = ""
        self._depth = 0
        self._item: dict | None = None
        self._text: list[str] = []
        # The left half of a configured pair, waiting for its right half.
        self._pending_header: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            if self._list:
                self._depth += 1
            else:
                name = _attr(attrs, "id")
                if name in (_AVAILABLE, _CONFIGURED, _CATALOGUE):
                    self._list = name
                    self._depth = 0
                    self._pending_header = None
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
                self._pending_header = None
            else:
                self._depth -= 1
            return
        if tag != "a" or self._item is None:
            return
        item, self._item = self._item, None
        label = " ".join("".join(self._text).split())
        self._text = []

        if self._list == _CATALOGUE:
            self._take_system_field(item, label)
            return

        # One of our CSV's columns, in either list that names them. The left
        # half of a configured pair carries data-left, as does every entry in
        # the available list.
        if item["user_column_id"] and (self._list == _AVAILABLE or item["left"]):
            try:
                self.columns.append((int(item["user_column_id"]), label))
            except ValueError:
                logger.debug(
                    "Millennium: unreadable user column id %r", item["user_column_id"]
                )
            else:
                if self._list == _CONFIGURED:
                    self._pending_header = label
            return

        # The right half of a configured pair: the field our column was
        # matched to. Rendered with no label, so it is identified by the
        # header it follows rather than by its own text.
        if self._list == _CONFIGURED and self._pending_header is not None:
            field = self._system_field(item, label)
            if field is not None:
                self.paired.append((self._pending_header, field))
            self._pending_header = None

    def _take_system_field(self, item: dict, label: str) -> None:
        if not label:
            return
        field = self._system_field(item, label)
        if field is not None:
            self.catalogue.append(field)

    @staticmethod
    def _system_field(item: dict, label: str) -> SystemField | None:
        if not item["id"]:
            return None
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
    """What the server understood about the CSV we just uploaded."""

    catalogue: dict[str, SystemField]
    columns: dict[str, int]
    paired: dict[str, SystemField]

    @classmethod
    def parse(cls, html: str) -> SelectFieldsPage:
        parser = _FieldsParser()
        parser.feed(html or "")
        page = cls(
            catalogue={f.label: f for f in parser.catalogue},
            columns={header: index for index, header in parser.columns},
            paired=dict(parser.paired),
        )
        if not page.catalogue and not page.paired:
            raise ImportFieldsUnavailable(
                "Millennium's import page listed no system fields — it is not "
                "the field-selection screen, or the page shape has changed"
            )
        if not page.columns:
            raise ImportFieldsUnavailable(
                "Millennium's import page listed none of our CSV's columns — "
                "the upload did not arrive as a CSV with a header row"
            )
        return page

    def field_for(self, header: str) -> SystemField | None:
        """The field a header feeds, preferring the server's own pairing.

        A pairing the server made is better evidence than one we would look
        up by name: it is what that install will actually do with the
        column, label collisions and all.
        """
        return self.paired.get(header) or self.catalogue.get(header)

    def mapping(self, headers: list[str]) -> list[dict]:
        """`ImportedFields` for step two, one entry per CSV column.

        The column index comes from the server's own
        ``data-user-column-id``, never from our position in the header row.
        The two agree on every capture we have; if they ever stop agreeing it
        is the server's number that decides which column it reads, so taking
        ours would write the right value into the wrong field — the one
        outcome worth engineering against.
        """
        missing = [h for h in headers if h not in self.columns]
        if missing:
            raise ImportFieldsUnavailable(
                "Millennium's import page did not read these CSV column(s): "
                f"{', '.join(missing)} — it read {', '.join(sorted(self.columns))}"
            )

        unknown = [
            h for h in headers
            if h not in IDENTITY_COLUMNS and self.field_for(h) is None
        ]
        if unknown:
            raise ImportFieldsUnavailable(
                "Millennium's import offers no field called "
                f"{', '.join(repr(h) for h in unknown)} on this install — the "
                "column labels have changed and nothing would be written"
            )

        out: list[dict] = []
        for header in headers:
            index = self.columns[header]
            if header in IDENTITY_COLUMNS:
                out.append({
                    "Card": 0,
                    "ID": IDENTITY_FIELD_ID,
                    "Name": "",
                    "TenantID": 0,
                    "UserColumnID": index,
                    "UserColumnName": header,
                })
                continue
            field = self.field_for(header)
            assert field is not None  # established by the `unknown` check
            out.append(field.mapped_to(index, header))
        return out

"""The four requests a cardholder import makes, and how we know it worked.

  step   request
  1      POST /DatabaseFunctions/ImportCardholders/SelectFields   (multipart)
  2      POST /DatabaseFunctions/ImportCardholders/ImportCardholdersNow
  3      GET  /api/LongOperationStatus?OperationType=CardholdersImport
  4      GET  /DatabaseFunctions/ImportCardholders/GetImportLogFile/{Context}

Every address, parameter and payload key here comes from a capture of the
live install. Two behaviours pinned below were learned by testing against it
and are easy to get wrong from a capture alone:

* the field identifiers run **First Name 1, Last Name 2, Card Type 3,
  Employee ID 4**, and an entry of identifier 1 with an empty name means
  "this column is deliberately unmapped" rather than a shared sentinel; and
* the import log is **never empty on success** — it ends with a summary of
  what it did, and that summary is the only positive evidence the rows were
  applied.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from agsync.lib.pacs.millennium_ultra import import_rows
from agsync.lib.pacs.millennium_ultra.client import (
    MAX_IMPORT_ROWS,
    RULE_UPDATE_MATCHED_ONLY,
    MillenniumError,
    MillenniumUltraClient,
)
from agsync.lib.pacs.millennium_ultra.import_fields import SelectFieldsPage

FIXTURES = pathlib.Path(__file__).parent / "millennium_fixtures"
SELECT_FIELDS = (FIXTURES / "import_select_fields.html").read_text()

LANDING = (
    '<form method="post"><input name="__RequestVerificationToken" '
    'type="hidden" value="landing-token"/></form>'
)
CONTEXT = "27-24"
EMPLOYEE = "22344"

# The install's own field list, read out of the captured page once.
CATALOGUE = SelectFieldsPage.parse(SELECT_FIELDS).catalogue


def _log(cardholders_updated=1, cards_updated=1, cardholders_added=0, cards_added=0):
    """An import log in the shape the live install produces."""
    return (
        "10/18/2026 4:33:23 PM  | Information | Start import cardholders\n"
        "10/18/2026 4:33:23 PM  | Information | Importing file contains 1 "
        "records and 2 columns\n"
        "10/18/2026 4:33:23 PM  | Information | Summary:\n"
        f"  {cardholders_added} cardholders added\n"
        f"  {cardholders_updated} cardholders updated\n"
        f"  {cards_added} cards added\n"
        f"  {cards_updated} cards updated\n\n"
        "10/18/2026 4:33:23 PM  | Information | Cardholders has been imported "
        "successfully!\n"
    )


def _status(**over) -> dict:
    status = {
        "CompanyID": 0, "OperatorID": 0, "Percent": 100, "OperationType": 2,
        "Error": "", "Success": True, "Completed": True, "Failed": False,
        "Context": CONTEXT,
    }
    status.update(over)
    return status


def _row(*slots: import_rows.SlotWrite, employee_id: str = EMPLOYEE):
    return import_rows.RowWrite(employee_id=employee_id, slots=slots)


def _header_of(multipart: str) -> list[str]:
    for line in multipart.split("\r\n"):
        if line.startswith("agsync_"):
            return line.split(",")
    raise AssertionError(f"no header row in the upload: {multipart[:400]}")


def _select_fields_page(headers: list[str], missing: str = "") -> str:
    """The field screen as the install renders it for these columns.

    Not a canned page. The real server sorts our columns into two lists,
    **placing** any header that exactly matches one of its own labels and
    removing that field from its catalogue. Our headers are aliases that
    match nothing, so they should all stay loose — and this mock reproduces
    the placing behaviour, so a regression in the aliasing gets caught.
    """
    catalogue = {k: v for k, v in CATALOGUE.items() if k != missing}
    available, placed = [], []
    for index, header in enumerate(headers):
        if header in catalogue:            # would be placed, and would vanish
            placed.append(
                '<a class="list-group-item col-xs-6" data-left="yes" href="#" '
                f'data-user-column-id="{index}">{header}</a>'
                '<a class="list-group-item col-xs-6" href="#" '
                'data-screen-number="0" data-id="1" data-card="0" '
                'data-tenant-id="0"></a>'
            )
            catalogue.pop(header)
        else:
            available.append(
                '<a class="list-group-item" href="#" data-left="yes" '
                f'data-user-column-id="{index}">{header}</a>'
            )
    listed = [
        '<a class="list-group-item" href="#" data-screen-number="2" '
        f'data-id="{f.field_id}" data-card="{f.card}" '
        f'data-tenant-id="{f.tenant_id}">{label}</a>'
        for label, f in catalogue.items()
    ]
    return f"""<!DOCTYPE html><html><body><form method="post" id="myForm">
      <div class="list-group fields-list" id="available_fields">{"".join(available)}</div>
      <div class="list-group fields-list" id="selected_fields">{"".join(listed)}</div>
      <div class="list-group fields-list" id="configured_fields">{"".join(placed)}</div>
    </form></body></html>"""


@pytest.fixture
def importer(monkeypatch):
    """A real client whose transport records what it was asked for."""
    def _build(statuses=None, log=None, select_fields=None, drop_field=""):
        seen: list[httpx.Request] = []
        queue = list(statuses or [_status()])
        body = _log() if log is None else log

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            path = request.url.path
            if path == "/DatabaseFunctions/ImportCardholders":
                return httpx.Response(200, text=LANDING)
            if path.endswith("/ImportCardholders/SelectFields"):
                if select_fields is not None:
                    return httpx.Response(200, text=select_fields)
                return httpx.Response(
                    200,
                    text=_select_fields_page(
                        _header_of(request.content.decode()), missing=drop_field
                    ),
                )
            if path.endswith("/ImportCardholdersNow"):
                return httpx.Response(200, json=True)
            if path == "/api/LongOperationStatus":
                return httpx.Response(200, json=queue.pop(0) if queue else _status())
            if path.startswith("/DatabaseFunctions/ImportCardholders/GetImportLogFile/"):
                return httpx.Response(200, text=body)
            return httpx.Response(404)

        client = MillenniumUltraClient(
            base_url="https://hosted105.mgiaccess.test", auth_cookie="session"
        )
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(client, "_pace", lambda: None)
        return client, seen

    return _build


def _only(seen, path) -> httpx.Request:
    matching = [r for r in seen if r.url.path == path]
    assert matching, f"no request to {path}; got {[r.url.path for r in seen]}"
    return matching[0]


def _started(seen) -> dict:
    return json.loads(
        _only(seen, "/DatabaseFunctions/ImportCardholders/ImportCardholdersNow").content
    )


# =====================================================================
# Where each step goes
# =====================================================================


def test_an_import_runs_all_four_steps_in_order(importer):
    client, seen = importer()

    outcome = client.import_cardholders([_row(import_rows.SlotWrite(2, active=False))])

    assert outcome.ok is True
    assert [r.url.path for r in seen] == [
        "/DatabaseFunctions/ImportCardholders",
        "/DatabaseFunctions/ImportCardholders/SelectFields",
        "/DatabaseFunctions/ImportCardholders/ImportCardholdersNow",
        "/api/LongOperationStatus",
        f"/DatabaseFunctions/ImportCardholders/GetImportLogFile/{CONTEXT}",
    ]


def test_the_poll_names_the_import_operation(importer):
    client, seen = importer()

    client.import_cardholders([_row(import_rows.SlotWrite(2, active=False))])

    poll = _only(seen, "/api/LongOperationStatus")
    assert poll.url.params["OperationType"] == "CardholdersImport"
    # The cache-buster. A stale "Completed" would have us read the previous
    # run's log and call this one clean.
    assert "_" in poll.url.params


def test_the_log_is_addressed_by_the_context_the_status_gave(importer):
    client, seen = importer(statuses=[_status(Context="31-7")])

    client.import_cardholders([_row(import_rows.SlotWrite(2, active=False))])

    assert any(r.url.path.endswith("/GetImportLogFile/31-7") for r in seen)


# =====================================================================
# What the job is asked to do
# =====================================================================


def test_the_rule_is_always_update_matched_only(importer):
    """The one rule that cannot create a cardholder.

    This integration writes cards onto records an operator already made. A
    row matching no Employee ID has to be a skip — under the rule that also
    adds, it would become a brand-new cardholder in the customer's system.
    """
    client, seen = importer()

    client.import_cardholders([_row(import_rows.SlotWrite(2, active=False))])

    assert _started(seen)["rule"] == RULE_UPDATE_MATCHED_ONLY == 2


def test_unknown_drop_down_entries_are_never_created(importer):
    client, seen = importer()

    client.import_cardholders([_row(import_rows.SlotWrite(2, active=True))])

    assert _started(seen)["ImportNewDropDownEntries"] is False


def test_the_file_is_announced_as_csv_with_a_header_row(importer):
    client, seen = importer()

    client.import_cardholders([_row(import_rows.SlotWrite(2, active=True))])

    started = _started(seen)
    assert started["format"] == 1
    assert started["HeaderIncluded"] is True
    assert started["Filename"] == "agsync-import.csv"


def test_the_upload_carries_the_file_and_the_anti_forgery_token(importer):
    client, seen = importer()

    client.import_cardholders([_row(import_rows.SlotWrite(2, active=False))])

    upload = _only(seen, "/DatabaseFunctions/ImportCardholders/SelectFields")
    body = upload.content.decode()
    assert "multipart/form-data" in upload.headers["content-type"]
    assert 'name="ImportFile"; filename="agsync-import.csv"' in body
    assert "Content-Type: text/csv" in body
    assert 'name="FileFormat"\r\n\r\n1' in body
    assert 'name="FieldHeaders"\r\n\r\ntrue' in body
    # Read off the landing page, which is the only reason that page is
    # fetched at all.
    assert 'name="__RequestVerificationToken"\r\n\r\nlanding-token' in body
    assert EMPLOYEE in body


def test_the_mapping_uses_the_real_field_identifiers(importer):
    """Employee ID is 4, not 1.

    A capture in which nothing was mapped suggested every identity column
    shared identifier 1. Testing against the install showed those were
    *unmapped* markers, and that the real identifiers run First Name 1, Last
    Name 2, Card Type 3, Employee ID 4. Getting this wrong writes a value
    into the wrong field.
    """
    client, seen = importer()

    client.import_cardholders([
        _row(
            import_rows.SlotWrite(
                2, card_number="5001", facility_code="66",
                card_format="AccessGrid / HID Wallet Format",
                activation="9/1/2026 12:00 AM", active=True,
            )
        )
    ])

    fields = {f["Name"]: f for f in _started(seen)["ImportedFields"]}
    assert fields["Employee ID"]["ID"] == 4
    assert fields["Employee ID"]["Card"] == 0
    assert fields["Card 2 Encoded Card No."]["ID"] == 16
    assert fields["Card 2 Encoded Card No."]["Card"] == 2
    assert fields["Card 2 Facility Code"]["ID"] == 21
    assert fields["Card 2 Card Format"]["ID"] == 20
    assert fields["Card 2 Activation Date"]["ID"] == 17
    assert fields["Card 2 Active"]["ID"] == 19


def test_nothing_is_sent_unmapped(importer):
    """Every column in the file is a column we are writing."""
    client, seen = importer()

    client.import_cardholders([_row(import_rows.SlotWrite(2, active=True))])

    for field in _started(seen)["ImportedFields"]:
        assert field["Name"], field


def test_the_name_and_card_type_are_never_mapped(importer):
    """The two fields whose inclusion did real damage in live testing."""
    client, seen = importer()

    client.import_cardholders([
        _row(import_rows.SlotWrite(1, card_number="5001", active=True))
    ])

    names = {f["Name"] for f in _started(seen)["ImportedFields"]}
    assert "First Name" not in names
    assert "Last Name" not in names
    assert "Card Type" not in names


def test_every_column_index_comes_from_the_server(importer):
    """Not from our position in the header row.

    If the two ever disagree it is the server's number that decides which
    column it reads, so taking ours would write the right value into the
    wrong field.
    """
    client, seen = importer()

    client.import_cardholders([
        _row(import_rows.SlotWrite(2, card_number="5001", active=True))
    ])

    upload = _only(seen, "/DatabaseFunctions/ImportCardholders/SelectFields")
    columns = _header_of(upload.content.decode())
    for field in _started(seen)["ImportedFields"]:
        assert columns[field["UserColumnID"]] == field["UserColumnName"]


# =====================================================================
# Refusals
# =====================================================================


def test_a_field_this_install_lacks_stops_the_import_before_it_starts(importer):
    """Nothing has been applied when this is noticed — the upload only
    stages the file — so refusing changes nothing in the PACS."""
    client, seen = importer(drop_field="Card 2 Facility Code")

    with pytest.raises(MillenniumError, match="no field called"):
        client.import_cardholders([
            _row(import_rows.SlotWrite(2, facility_code="66"))
        ])

    assert not any(r.url.path.endswith("ImportCardholdersNow") for r in seen)


def test_a_page_that_is_not_the_field_screen_is_refused(importer):
    client, _ = importer(select_fields="<html><body>Nope</body></html>")

    with pytest.raises(MillenniumError, match="listed no system fields"):
        client.import_cardholders([_row(import_rows.SlotWrite(2, active=True))])


def test_more_rows_than_the_cap_are_refused_without_a_request(importer):
    """An import applies every row in the file, so a cycle that wants to
    change far more cards than a cycle ever does has misread something."""
    client, seen = importer()
    rows = [
        _row(import_rows.SlotWrite(2, active=False), employee_id=str(n))
        for n in range(MAX_IMPORT_ROWS + 1)
    ]

    with pytest.raises(MillenniumError, match="Refusing to import"):
        client.import_cardholders(rows)

    assert seen == []


def test_no_rows_is_a_no_op(importer):
    client, seen = importer()

    outcome = client.import_cardholders([])

    assert (outcome.ok, outcome.rows) == (True, 0)
    assert seen == []


def test_a_failed_job_raises_with_millenniums_own_words(importer):
    client, _ = importer(
        statuses=[_status(Failed=True, Success=False, Error="Row 1: bad date")],
        log="line 1: could not parse 31/31/2027",
    )

    with pytest.raises(MillenniumError, match="bad date"):
        client.import_cardholders([_row(import_rows.SlotWrite(2, active=True))])


def test_a_job_that_never_finishes_is_not_reported_as_applied(importer, monkeypatch):
    monkeypatch.setattr(
        "agsync.lib.pacs.millennium_ultra.client.IMPORT_TIMEOUT_S", 0.0
    )
    client, _ = importer(statuses=[_status(Completed=False)])

    with pytest.raises(MillenniumError, match="did not finish"):
        client.import_cardholders([_row(import_rows.SlotWrite(2, active=True))])


# =====================================================================
# Knowing it actually landed
# =====================================================================


def test_matching_summary_counts_are_what_make_it_successful(importer):
    client, _ = importer(log=_log(cardholders_updated=1, cards_updated=1))

    outcome = client.import_cardholders([_row(import_rows.SlotWrite(2, active=True))])

    assert outcome.ok is True
    assert outcome.summary is not None
    assert outcome.summary.cards_updated == 1


def test_a_row_that_matched_nothing_is_not_reported_as_applied(importer, caplog):
    """Millennium reports a run that matched nothing as a success.

    The rule we send makes an unmatched Employee ID a skip rather than an
    error, so success on its own is not evidence of anything. The summary is,
    and claiming otherwise would record a card we never wrote.
    """
    client, _ = importer(log=_log(cardholders_updated=0, cards_updated=0))

    with caplog.at_level("ERROR"):
        outcome = client.import_cardholders(
            [_row(import_rows.SlotWrite(2, active=True))]
        )

    assert outcome.ok is False
    assert "matched no Employee ID" in caplog.text


def test_a_cardholder_being_added_raises_the_alarm(importer, caplog):
    """Under the rule we send this should be impossible.

    If it happens, the rule that ran was not the rule we sent, and nothing
    can be assumed about what else the import did.
    """
    client, _ = importer(log=_log(cardholders_added=1))

    with caplog.at_level("ERROR"):
        outcome = client.import_cardholders(
            [_row(import_rows.SlotWrite(2, active=True))]
        )

    assert outcome.ok is False
    assert "ADDED 1 cardholder" in caplog.text


def test_fewer_cards_updated_than_expected_is_not_success(importer):
    client, _ = importer(log=_log(cardholders_updated=1, cards_updated=1))

    outcome = client.import_cardholders([
        _row(
            import_rows.SlotWrite(1, active=True),
            import_rows.SlotWrite(2, active=True),
        )
    ])

    assert outcome.ok is False


def test_a_log_without_a_summary_is_not_success(importer):
    """No counts means no evidence, whatever the status said."""
    client, _ = importer(log="Start import cardholders\nsomething happened\n")

    outcome = client.import_cardholders([_row(import_rows.SlotWrite(2, active=True))])

    assert outcome.ok is False

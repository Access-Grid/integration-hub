"""The four requests a cardholder import actually makes.

This is the write path, so the tests are about the two things that could go
wrong quietly: sending the wrong rule, and sending a field mapping built on
a guess. Both are invisible in a 200 — Millennium answers a successful job
and an import that matched nothing the same way.

  step   request
  1      POST /DatabaseFunctions/ImportCardholders/SelectFields   (multipart)
  2      POST /DatabaseFunctions/ImportCardholders/ImportCardholdersNow (JSON)
  3      GET  /api/LongOperationStatus?OperationType=CardholdersImport
  4      GET  /DatabaseFunctions/ImportCardholders/GetImportLogFile/{Context}

Every URL, parameter and JSON key here is from a capture of the live install
on 2026-10-16, down to the shape of the `Context` ("27-24").
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
from agsync.lib.pacs.millennium_ultra.import_fields import (
    IDENTITY_COLUMNS,
    SelectFieldsPage,
)

FIXTURES = pathlib.Path(__file__).parent / "millennium_fixtures"
SELECT_FIELDS = (FIXTURES / "import_select_fields.html").read_text()

LANDING = (
    '<form method="post"><input name="__RequestVerificationToken" '
    'type="hidden" value="landing-token"/></form>'
)
CONTEXT = "27-24"

# The install's own field list, read out of the captured page once.
CATALOGUE = SelectFieldsPage.parse(SELECT_FIELDS).catalogue


def _header_of(multipart: str) -> list[str]:
    """The CSV header row out of an upload's multipart body."""
    for line in multipart.split("\r\n"):
        if line.startswith("First Name,") or line == "First Name":
            return line.split(",")
    raise AssertionError(f"no CSV header in the upload: {multipart[:400]}")


def _select_fields_page(headers: list[str], missing: str = "") -> str:
    """The field screen as the install renders it for these columns.

    Not a canned page: the real server sorts the uploaded CSV's columns into
    the two lists, pairing up whatever matches a system field by name and
    leaving the rest loose — and a column it paired is *removed* from the
    catalogue, which is the case a canned page would never produce. Our
    writer names its columns after the fields they feed, so that is the case
    that matters.
    """
    catalogue_fields = {k: v for k, v in CATALOGUE.items() if k != missing}
    paired, available, used = [], [], set()
    for index, header in enumerate(headers):
        left = (
            '<a class="list-group-item col-xs-6" data-left="yes" href="#" '
            f'data-user-column-id="{index}">{header}</a>'
        )
        if header in IDENTITY_COLUMNS:
            paired.append(left)
            paired.append(
                '<a class="list-group-item col-xs-6" href="#" '
                'data-screen-number="0" data-id="1" data-card="0" '
                'data-tenant-id="0"></a>'
            )
        elif header in catalogue_fields:
            field = catalogue_fields[header]
            used.add(header)
            paired.append(left)
            paired.append(
                '<a class="list-group-item col-xs-6" href="#" '
                f'data-screen-number="2" data-id="{field.field_id}" '
                f'data-card="{field.card}" '
                f'data-tenant-id="{field.tenant_id}"></a>'
            )
        else:
            available.append(
                '<a class="list-group-item" href="#" data-left="yes" '
                f'data-user-column-id="{index}">{header}</a>'
            )

    catalogue = [
        '<a class="list-group-item" href="#" data-screen-number="2" '
        f'data-id="{f.field_id}" data-card="{f.card}" '
        f'data-tenant-id="{f.tenant_id}">{label}</a>'
        for label, f in catalogue_fields.items() if label not in used
    ]
    return f"""<!DOCTYPE html><html><body>
      <form method="post" id="myForm">
        <div class="list-group fields-list" id="available_fields">
          {"".join(available)}
        </div>
        <div class="list-group fields-list" id="selected_fields">
          {"".join(catalogue)}
        </div>
        <div class="list-group fields-list" id="configured_fields">
          {"".join(paired)}
        </div>
      </form>
    </body></html>"""


def _status(**over) -> dict:
    status = {
        "CompanyID": 0, "OperatorID": 0, "Percent": 100, "OperationType": 2,
        "Error": "", "Success": True, "Completed": True, "Failed": False,
        "Context": CONTEXT,
    }
    status.update(over)
    return status


def _row(*slots: import_rows.SlotWrite) -> import_rows.RowWrite:
    return import_rows.RowWrite(
        identity=import_rows.Identity("Kai", "Mowatt"), slots=slots
    )


@pytest.fixture
def importer(monkeypatch):
    """A real client whose transport records what it was asked for."""
    def _build(statuses=None, log="", select_fields=None, drop_field=""):
        seen: list[httpx.Request] = []
        queue = list(statuses or [_status()])

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
                return httpx.Response(200, text=log)
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
    assert outcome.rows == 1
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
    # The UI's own cache-buster. A cached "Completed" would have us read a
    # previous run's log and call this one clean.
    assert "_" in poll.url.params


def test_the_log_is_addressed_by_the_context_the_status_gave(importer):
    client, seen = importer(statuses=[_status(Context="31-7")])

    client.import_cardholders([_row(import_rows.SlotWrite(2, active=False))])

    assert any(
        r.url.path.endswith("/GetImportLogFile/31-7") for r in seen
    ), [r.url.path for r in seen]


# =====================================================================
# What the job is asked to do
# =====================================================================


def test_the_rule_is_always_update_matched_only(importer):
    """The one rule that cannot create a cardholder.

    This integration writes cards onto records an operator already made. A
    row whose name matches nothing has to be a skip — under rule 3 it would
    be a brand-new duplicate cardholder, in the customer's own system, named
    after somebody who is already in it.
    """
    client, seen = importer()

    client.import_cardholders([_row(import_rows.SlotWrite(2, active=False))])

    assert _started(seen)["rule"] == RULE_UPDATE_MATCHED_ONLY == 2


def test_unknown_drop_down_entries_are_never_created(importer):
    """Every value we send was read back out of this install.

    So a value it does not recognise means we have misread something — and
    creating it would make that permanent, in their configuration.
    """
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
    assert "Card 2 Active" in body


def test_the_mapping_is_read_off_the_page_not_guessed(importer):
    """The numeric field ids come from the install, every time.

    A hard-coded map is how `FaciltyCode` cost us a day: the name was wrong,
    nothing checked it, and the write silently did nothing.
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

    fields = {f["UserColumnName"]: f for f in _started(seen)["ImportedFields"]}
    # The identity columns, against the sentinel the capture pairs them with.
    for name in ("First Name", "Last Name", "Card Type"):
        assert fields[name]["ID"] == 1
        assert fields[name]["Name"] == ""
        assert fields[name]["Card"] == 0
    # And the slot's own fields, with the ids and card number this install
    # declares for them.
    assert fields["Card 2 Encoded Card No."]["ID"] == 16
    assert fields["Card 2 Encoded Card No."]["Card"] == 2
    assert fields["Card 2 Facility Code"]["ID"] == 21
    assert fields["Card 2 Card Format"]["ID"] == 20
    assert fields["Card 2 Active"]["ID"] == 19
    assert fields["Card 2 Activation Date"]["ID"] == 17


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
    header = upload.content.decode().split("\r\n")
    header = next(line for line in header if line.startswith("First Name"))
    columns = header.split(",")
    for field in _started(seen)["ImportedFields"]:
        assert columns[field["UserColumnID"]] == field["UserColumnName"]


# =====================================================================
# Refusals
# =====================================================================


def test_a_file_the_page_cannot_carry_is_refused_before_the_job_starts(importer):
    """A column this install has no field for stops the whole import.

    Nothing has been applied at the point this is noticed — the upload only
    stages the file — so refusing here changes nothing in the PACS.
    """
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
    """The blast radius, stated once.

    A cardholder save can only ever damage the one record it addresses. An
    import applies every row in the file, so a cycle that wants to change
    far more cards than a cycle ever does has misread something.
    """
    client, seen = importer()
    rows = [
        import_rows.RowWrite(
            identity=import_rows.Identity(f"First{n}", "Last"),
            slots=(import_rows.SlotWrite(2, active=False),),
        )
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
# The log
# =====================================================================


def test_a_clean_run_leaves_an_empty_log(importer):
    client, _ = importer(log="")

    assert client.import_cardholders(
        [_row(import_rows.SlotWrite(2, active=True))]
    ).log == ""


def test_a_run_with_a_log_is_not_reported_as_applied(importer, caplog):
    """Millennium reports a run that matched nothing as a success.

    The rule we send makes an unmatched row a skip rather than an error, so
    `Success: true` is not on its own evidence that anything was applied —
    the log is the only place a skip appears. Claiming it anyway would have
    the ledger record a card we never wrote, and the ledger is what later
    decides whether an operator revoked something.
    """
    client, _ = importer(log="Row 1: no matching cardholder - skipped")

    with caplog.at_level("WARNING"):
        outcome = client.import_cardholders(
            [_row(import_rows.SlotWrite(2, active=True))]
        )

    assert outcome.ok is False
    assert "no matching cardholder" in outcome.log
    assert "no matching cardholder" in caplog.text


def test_a_log_that_cannot_be_fetched_does_not_lose_the_outcome(importer):
    """By the time the log is read, the import has already happened."""
    client, _ = importer(statuses=[_status(Context="")])

    outcome = client.import_cardholders([_row(import_rows.SlotWrite(2, active=True))])

    assert outcome.ok is True
    assert outcome.log == ""

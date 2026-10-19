"""The five requests an import makes, as the import screen makes them.

  token    GET  /DatabaseFunctions/ImportCardholders
  upload   POST /DatabaseFunctions/ImportCardholders/SelectFields   (multipart)
  run      POST /DatabaseFunctions/ImportCardholders/ImportCardholdersNow (JSON)
  poll     GET  /api/LongOperationStatus?operationtype=CardholdersImport
  log      GET  /DatabaseFunctions/ImportCardholders/GetImportLogFile/{Context}
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from agsync.lib.pacs.millennium_ultra.client import MillenniumError, MillenniumUltraClient
from agsync.lib.pacs.millennium_ultra.import_file import Row, build

FIXTURES = pathlib.Path(__file__).parent / "millennium_fixtures"
TOKEN = "form-token-123"
INDEX = f'<form><input name="__RequestVerificationToken" type="hidden" value="{TOKEN}"/></form>'
SELECT_FIELDS = '<div class="list-group fields-list" id="selected_fields"></div>'
SUCCESS_LOG = (FIXTURES / "import_log_cards_skipped.txt").read_text()


def _status(**over):
    status = {"Percent": 100, "OperationType": 2, "Error": "", "Success": True,
              "Completed": True, "Failed": False, "Context": "27-21"}
    status.update(over)
    return status


@pytest.fixture
def importer(monkeypatch):
    """A real client whose transport records what it was asked for."""
    def _build(*, upload_page=SELECT_FIELDS, started=True, log=SUCCESS_LOG):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            path = request.url.path
            if path == "/DatabaseFunctions/ImportCardholders":
                return httpx.Response(200, text=INDEX)
            if path.endswith("/SelectFields"):
                return httpx.Response(200, text=upload_page)
            if path.endswith("/ImportCardholdersNow"):
                return httpx.Response(200, json=started)
            if path == "/api/LongOperationStatus":
                return httpx.Response(200, json=_status())
            if path.endswith("/GetImportLogFile/27-21"):
                return httpx.Response(200, text=log)
            return httpx.Response(404)

        client = MillenniumUltraClient(
            base_url="https://hosted105.mgiaccess.test", auth_cookie="session"
        )
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(client, "_pace", lambda: None)
        return client, seen

    return _build


def _file():
    cells = {(2, "Active"): "False", (2, "Encoded Card No."): "5001", (2, "Facility Code"): "66"}
    [imported] = build([Row("22344", "King", "Bunsen", cells)])
    return imported


def test_the_five_requests_in_order(importer):
    client, seen = importer()
    client.import_cardholders(_file())
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/DatabaseFunctions/ImportCardholders"),
        ("POST", "/DatabaseFunctions/ImportCardholders/SelectFields"),
        ("POST", "/DatabaseFunctions/ImportCardholders/ImportCardholdersNow"),
        ("GET", "/api/LongOperationStatus"),
        ("GET", "/DatabaseFunctions/ImportCardholders/GetImportLogFile/27-21"),
    ]
    assert seen[3].url.params["operationtype"] == "CardholdersImport"


def test_the_upload_carries_the_file_and_the_pages_token(importer):
    client, seen = importer()
    imported = _file()
    client.import_cardholders(imported)
    body = seen[1].content.decode()
    assert f'name="__RequestVerificationToken"\r\n\r\n{TOKEN}' in body
    assert 'name="ImportFile"; filename="agsync-import.csv"' in body
    assert imported.csv in body


def test_the_run_updates_only_and_maps_our_columns(importer):
    """Rule 2 can never add a cardholder, whatever Employee ID a row names."""
    client, seen = importer()
    imported = _file()
    client.import_cardholders(imported)
    run = json.loads(seen[2].content)
    assert run["rule"] == 2
    assert run["Filename"] == "agsync-import.csv"
    assert run["ImportedFields"] == imported.fields


def test_the_log_comes_back_with_its_warnings(importer):
    client, _ = importer()
    log = client.import_cardholders(_file())
    assert log.counts["cards skipped"] == 2
    assert log.skipped() == {"22344"}


def test_an_error_in_the_log_raises(importer):
    client, _ = importer(log=(FIXTURES / "import_log_error_then_success.txt").read_text())
    with pytest.raises(MillenniumError, match="Necessary column"):
        client.import_cardholders(_file())


def test_an_upload_that_does_not_reach_the_mapping_page_raises(importer):
    client, seen = importer(upload_page="<html>Something went wrong</html>")
    with pytest.raises(MillenniumError, match="did not accept the import file"):
        client.import_cardholders(_file())
    assert len(seen) == 2, "nothing is run after a refused upload"


def test_a_refused_start_raises(importer):
    client, _ = importer(started=False)
    with pytest.raises(MillenniumError, match="refused to start the import"):
        client.import_cardholders(_file())

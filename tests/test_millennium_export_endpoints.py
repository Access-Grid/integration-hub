"""The three requests a bulk export actually makes.

The export is a queued job: start it, poll until it says it finished, then
fetch the file. Two of those three moved when the install went to ASP.NET
Core, and nothing here noticed — every existing export test works on the
parsing or drives a fake client, so the endpoints themselves were never
pinned and the sync quietly fell back to reading ~1800 detail pages a
cycle instead.

  step        classic                                  Core
  start       /DatabaseFunctions/.../ExportCardholdersNow   unchanged
  poll        POST /Home/GetLongOperationStatus        GET /api/LongOperationStatus
  fetch       GET /DatabaseFunctions/.../GetExportFile/{name}
                                                       GET /WebServices/FileDownload/
"""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from agsync.lib.pacs.millennium_ultra.client import (
    MillenniumError,
    MillenniumUltraClient,
)

CSV = (
    '"First Name","Last Name","Employee ID","Phone","E-Mail","Current Status",'
    '"Encoded Card No.","Activation Date","Expiration Date","Active",'
    '"Card Format","Facility Code"\r\n'
    '"Greg","Dorvil","11665","","","Active","74","","","True","8","99"\r\n'
)
ARCHIVE_NAME = "CardholderExport_27_21.zip"


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as bundle:
        bundle.writestr("CardholderExport.csv", CSV)
    return buf.getvalue()


def _status(**over) -> dict:
    status = {
        "CompanyID": 0, "OperatorID": 0, "Percent": 100, "OperationType": 1,
        "Error": "", "Success": True, "Completed": True, "Failed": False,
        "Context": ARCHIVE_NAME,
    }
    status.update(over)
    return status


@pytest.fixture
def exporter(monkeypatch):
    """A real client whose transport records what it was asked for."""
    def _build(statuses=None):
        seen: list[httpx.Request] = []
        queue = list(statuses or [_status()])

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            path = request.url.path
            if path.endswith("/ExportCardholdersNow"):
                return httpx.Response(200, json=True)
            if path == "/api/LongOperationStatus":
                return httpx.Response(200, json=queue.pop(0) if queue else _status())
            if path == "/WebServices/FileDownload/":
                return httpx.Response(200, content=_zip())
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


# =====================================================================
# Where each step goes
# =====================================================================


def test_an_export_comes_back_as_the_csv_from_the_zip(exporter):
    client, _ = exporter()

    csv_text = client.export_cardholders()

    assert "Dorvil" in csv_text
    assert csv_text.startswith('"First Name"')


def test_the_status_is_a_get_under_api(exporter):
    client, seen = exporter()
    client.export_cardholders()

    poll = _only(seen, "/api/LongOperationStatus")
    assert poll.method == "GET"
    assert poll.url.params["operationtype"] == "CardholdersExport"


def test_the_status_is_asked_for_as_an_xhr(exporter):
    client, seen = exporter()
    client.export_cardholders()

    assert _only(seen, "/api/LongOperationStatus").headers["X-Requested-With"] == (
        "XMLHttpRequest"
    )


def test_the_status_is_never_served_from_a_cache(exporter):
    """A stale "Completed" would download the previous run's file."""
    client, seen = exporter(statuses=[_status(Completed=False), _status()])
    client.export_cardholders()

    polls = [r for r in seen if r.url.path == "/api/LongOperationStatus"]
    assert len(polls) == 2
    assert all("_" in r.url.params for r in polls)


def test_the_file_is_fetched_by_kind_not_by_name(exporter):
    """The Core build has no route that takes the archive's filename."""
    client, seen = exporter()
    client.export_cardholders()

    fetch = _only(seen, "/WebServices/FileDownload/")
    assert fetch.url.params["FileType"] == "CardholdersExport"
    assert ARCHIVE_NAME not in str(fetch.url)


def test_no_request_goes_to_a_retired_endpoint(exporter):
    client, seen = exporter()
    client.export_cardholders()

    paths = [r.url.path for r in seen]
    assert "/Home/GetLongOperationStatus" not in paths
    assert not any("GetExportFile" in p for p in paths)


def test_starting_the_job_is_unchanged(exporter):
    """The one step that did not move."""
    client, seen = exporter()
    client.export_cardholders()

    start = _only(seen, "/DatabaseFunctions/ExportCardholders/ExportCardholdersNow")
    assert start.method == "POST"
    body = json.loads(start.content)
    assert body["IncudeImages"] is False, "Millennium's own spelling"
    assert body["cards"] == [1, 2, 3]


# =====================================================================
# Refusing rather than guessing
# =====================================================================


def test_a_job_that_names_no_file_is_an_error(exporter):
    """Downloading anyway would fetch whatever the last run left behind."""
    client, seen = exporter(statuses=[_status(Context="")])

    with pytest.raises(MillenniumError, match="without naming its file"):
        client.export_cardholders()

    assert not [r for r in seen if r.url.path == "/WebServices/FileDownload/"]


def test_a_failed_job_is_not_downloaded(exporter):
    client, seen = exporter(statuses=[_status(Failed=True, Error="disk full")])

    with pytest.raises(MillenniumError, match="disk full"):
        client.export_cardholders()

    assert not [r for r in seen if r.url.path == "/WebServices/FileDownload/"]

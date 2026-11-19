"""A failure anywhere in the sync loop is debuggable from the log alone:
which phase, which pass, what AccessGrid answered, and a traceback."""

from __future__ import annotations

import logging

import pytest
import requests

from agsync import ag as ag_module
from agsync.ag import AccessGridError

from .test_unity_spec_end_to_end import _desfire, _status, mailed, world  # noqa: F401


def _switched_off(world):  # noqa: F811
    unity, ag, cycle = _desfire(world, False)
    cycle()
    _status(unity.tokens["a"][0], "Inactive")  # phase 2 will suspend the pass
    return ag, cycle, ag.only_pass()


def test_a_crash_inside_a_phase_names_the_phase_and_the_pass(world, mailed, caplog):  # noqa: F811
    ag, cycle, pass_id = _switched_off(world)

    def broken(card_id):
        raise ValueError("boom")

    ag.suspend = broken
    caplog.set_level(logging.INFO)
    result = cycle(may_fail=True)
    where = f"phase 2 (PACS → AccessGrid status), on a/t1 (Jim Cooper), pass {pass_id}"
    assert f"Sync cycle stopped in {where} — the phases after it did not run this cycle: ValueError: boom" \
        in caplog.text
    assert result.error == f"{where}: ValueError: boom"
    [record] = [r for r in caplog.records if r.getMessage().startswith("Sync cycle stopped in")]
    assert record.exc_info, "with a traceback"


def test_an_accessgrid_refusal_in_a_phase_names_the_pass_with_a_traceback(world, mailed, caplog):  # noqa: F811
    ag, cycle, pass_id = _switched_off(world)

    def refused(card_id):
        raise AccessGridError("API request failed: card is locked")

    ag.suspend = refused
    caplog.set_level(logging.INFO)
    assert cycle().error is None, "one pass failing does not stop the cycle"
    [record] = [r for r in caplog.records if "Failed to push" in r.getMessage()]
    assert record.getMessage() == (f"  Failed to push a/t1 (Jim Cooper)'s PACS status (suspended) to AG card "
                                   f"{pass_id} (AccessGrid state created): API request failed: card is locked")
    assert record.exc_info


def _response(status: int, body: str) -> requests.Response:
    r = requests.Response()
    r.status_code, r._content = status, body.encode()
    return r


def test_a_failed_accessgrid_call_logs_method_path_status_and_answer(monkeypatch, caplog):
    monkeypatch.setattr(requests, "request", lambda method, url, **kw: _response(422, '{"message": "bad\n  phone"}'))
    caplog.set_level(logging.INFO)
    resp = ag_module._LoggedRequests.request(
        "PATCH", "https://api.accessgrid.com/v1/key-cards/abc?sig_payload=%7B%7D", json={"email": "x@y.z"})
    assert resp.status_code == 422, "the SDK still sees the answer"
    [record] = caplog.records
    message = record.getMessage()
    assert message.startswith("AccessGrid API: PATCH /v1/key-cards/abc -> HTTP 422 after ")
    assert message.endswith('{"message": "bad phone"}')
    assert "sig_payload" not in message and "x@y.z" not in message, "no query string, no request body"


def test_an_accessgrid_call_with_no_answer_is_logged_and_still_raised(monkeypatch, caplog):
    def down(method, url, **kw):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "request", down)
    with pytest.raises(requests.exceptions.ConnectionError):
        ag_module._LoggedRequests.request("GET", "https://api.accessgrid.com/v1/key-cards")
    assert "AccessGrid API: GET /v1/key-cards failed after" in caplog.text
    assert "no answer: ConnectionError: refused" in caplog.text


def test_the_sdk_sends_its_requests_through_the_logging_layer():
    from accessgrid import client as sdk

    assert sdk.requests is ag_module._LoggedRequests
    assert sdk.requests.exceptions is requests.exceptions, "the SDK's own except clauses still match"

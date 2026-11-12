"""The probe carries its own copy of what the Unity adapter reads and writes,
so the exe can run without the rest of the hub. These keep the copy honest."""

import importlib.util
from pathlib import Path

from agsync.lib.pacs.avigilon import adapter, client

_spec = importlib.util.spec_from_file_location(
    "avigilon_probe", Path(__file__).parent.parent / "scripts" / "avigilon_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_probe_expects_the_fields_the_client_writes():
    assert probe.TOKEN_FORM_FIELDS == client.TOKEN_FORM_FIELDS
    assert probe.TOKEN_FORM_DATES == client.TOKEN_FORM_DATES
    assert probe._PREFIXES == client._PREFIXES


def test_probe_reads_statuses_dates_and_searches_as_the_adapter_does():
    assert probe.STATUS_CODES == client._STATUS_CODES
    assert probe.DATE_FORMATS == adapter._DATE_FORMATS
    assert probe._XML_SEARCH == client.XML_IDENTITY_SEARCH
    assert probe._EMBOSSED_SEARCH == client.EMBOSSED_SEARCH
    assert probe._INTERNAL_SEARCH == client.INTERNAL_SEARCH
    assert probe._XML_PAGE == client.XML_PAGE_SIZE


def test_detail_mode_compares_presence_and_agreement_never_values():
    import json

    import httpx

    listed = {"1": {"plasecidentityTitle": "Coach", "plasecidentityEmailaddress": "ann@x.test"},
              "2": {"plasecidentityDepartment": "Office"}}
    detail = {"1": {"plasecidentityTitle": "Coach", "plasecidentityEmailaddress": "ann@x.test"},
              "2": {}}  # a detail that lacks what the list has

    def row(cn, fields):
        return f"<identity><cn>{cn}</cn>" + "".join(f"<{k}>{v}</{k}>" for k, v in fields.items()) + "</identity>"

    def serve(req):
        path, q = req.url.path, dict(req.url.params)
        if path == "/identities.xml":
            ids = [q["id"]] if q.get("id") else ["0", "9", "1", "2"]
            rows = {"0": {}, "9": {}, **listed}
            return httpx.Response(200, text="<identities>" + "".join(row(i, rows[i]) for i in ids) + "</identities>")
        cn, ext = path.rsplit("/", 1)[1].split(".")
        if ext == "json":
            return httpx.Response(200, json={"data": {"id": cn, "attributes": {"cn": cn, **detail[cn]}}})
        return httpx.Response(200, text="<identities>" + row(cn, detail[cn]) + "</identities>")

    p = probe.Probe("unity", "u", "pw", mode="detail")
    p.http = httpx.Client(base_url="https://unity", transport=httpx.MockTransport(serve))
    p.detail_vs_list(samples=2)
    out = p.report["detail_vs_list"]
    assert list(p.report)[4] == "detail_vs_list"
    assert out["cardholders_compared"] == 2
    assert out["summary"]["identityTitle"] == {"list": 1, "detail_json": 1, "detail_xml": 1,
                                               "json_agrees": 2, "xml_agrees": 2}
    assert out["summary"]["identityDepartment"] == {"list": 1, "detail_json": 0, "detail_xml": 0,
                                                    "json_agrees": 1, "xml_agrees": 1}
    assert out["list_search_by_id"] == {"answered": True, "rows": 1, "only_that_identity": True}
    text = json.dumps(out)
    assert "Coach" not in text and "Office" not in text and "ann@x.test" not in text


# -- full mode: does the hub accept each holder's token list? ------------------

import json as _json  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from .test_avigilon_unity import FakeUnity, _client, _identity, _token  # noqa: E402

# Every token list the client is tested to reject (test_unity_malformed_tokens),
# a few it accepts, and the edges between.
_JSON_ITEMS = [
    [None], [42], [_token("known"), None], [{}],
    [{"id": "  "}], [{"id": []}], [{"id": True}],
    [{"id": "known", "attributes": None}], [{"id": "known"}],
    [{"id": "known", "extended_attributes": ["broken"]}],
    [{"id": "known", "extended_attributes": []}],
    [_token("known", status="unknown")], [_token("known", status="Activo")],
    [_token("known"), _token("known")],
    [], [_token("known")], [_token("a1"), _token("a2", status="Inactive")],
    [_token("a1", status="Not Yet Active"), _token("a2", status="Expired")],
    [_token(f"t{i}") for i in range(500)],
]
_JSON_META = [
    None, {}, {"recordsTotal": 1}, {"recordsFiltered": 1, "recordsTotal": 1}, {"recordsTotal": "1"},
    {"recordsTotal": 2}, {"recordsTotal": 0}, {"recordsTotal": -1}, {"recordsTotal": "unknown"},
    {"recordsTotal": True}, {"recordsTotal": 1.5}, {"recordsFiltered": 2, "recordsTotal": 1},
    {"recordsFiltered": 1, "recordsTotal": 2}, {"recordsFiltered": 0, "recordsTotal": 1},
    ["broken"], [], False,
]
_XML = [
    "<tokens><unexpected/></tokens>", "<tokens>broken</tokens>",
    "<tokens><token/></tokens>", "<tokens><token><cn> </cn></token></tokens>",
    "<tokens><token><cn>known</cn></token><unexpected/></tokens>",
    "<tokens/>", "<nil-classes/>", "<nil-classes><x/></nil-classes>", "<html>sign in</html>", "not xml",
    "<tokens><token><cn>t1</cn><plasecTokenstatus>Active</plasecTokenstatus></token></tokens>",
    "<token><cn>t1</cn><plasecTokenstatus>2</plasecTokenstatus></token>",
    "<tokens><token><cn>t1</cn><plasecTokenstatus>Activo</plasecTokenstatus></token></tokens>",
    "<tokens><token><cn>t1</cn><plasecTokenstatus>Active</plasecTokenstatus></token>"
    "<token><cn>t1</cn><plasecTokenstatus>Active</plasecTokenstatus></token></tokens>",
]


def _client_accepts(raw: dict) -> bool:
    unity = FakeUnity([_identity("a")])
    for fmt in ("json", "xml"):
        path = f"/identities/a/tokens.{fmt}"
        if fmt in raw:
            unity.raw[path] = raw[fmt]
        else:
            unity.fail[path] = 503
    try:
        _client(unity).list_tokens("a")
    except Exception:  # noqa: BLE001 — any refusal is a refusal
        return False
    return True


@pytest.mark.parametrize("items", _JSON_ITEMS, ids=range(len(_JSON_ITEMS)))
@pytest.mark.parametrize("meta", _JSON_META[:6], ids=range(6))
def test_probe_judges_a_json_token_list_as_the_client_does(items, meta):
    body = {"tokens": items} if meta is None else {"tokens": items, "meta": meta}
    problem, _ = probe.json_token_read_problem(body)
    assert (problem is None) == _client_accepts({"json": _json.dumps(body)}), problem


@pytest.mark.parametrize("meta", _JSON_META, ids=range(len(_JSON_META)))
def test_probe_judges_json_counts_as_the_client_does(meta):
    body = {"tokens": [_token("known")], "meta": meta}
    problem, _ = probe.json_token_read_problem(body)
    assert (problem is None) == _client_accepts({"json": _json.dumps(body)}), problem


@pytest.mark.parametrize("xml", _XML, ids=range(len(_XML)))
def test_probe_judges_an_xml_token_list_as_the_client_does(xml):
    problem, _ = probe.xml_token_read_problem(xml)
    assert (problem is None) == _client_accepts({"xml": xml}), problem


def test_full_mode_reads_only_and_counts_holders_the_hub_could_not_read(monkeypatch):
    good = {"tokens": [_token("t1", embossed="accessgrid")], "meta": {"recordsTotal": 1}}
    bad_json = {"tokens": [_token("t1", status="Activo")]}
    bad_xml = "<tokens><token><cn>t1</cn><plasecTokenstatus>Activo</plasecTokenstatus></token></tokens>"
    sent = []

    def serve(req):
        sent.append((req.method, req.url.path))
        path, q = req.url.path, dict(req.url.params)
        if path == "/identities.xml":
            ids = ["good", "bad"] if q.get("adv_search_val_0") == "accessgrid" else []
            return httpx.Response(200, text="<identities>" + "".join(
                f"<identity><cn>{i}</cn></identity>" for i in ids) + "</identities>")
        if path == "/identities/good/tokens.json":
            assert q == {"page": "1", "perpage": "500"}
            return httpx.Response(200, json=good)
        if path == "/identities/bad/tokens.json":
            return httpx.Response(200, json=bad_json)
        if path == "/identities/bad/tokens.xml":
            return httpx.Response(200, text=bad_xml)
        return httpx.Response(404, text="nope")

    p = probe.Probe("unity", "u", "pw", mode="full")
    p.http = httpx.Client(base_url="https://unity", transport=httpx.MockTransport(serve))
    monkeypatch.setattr(p, "accessgrid", lambda: None)
    monkeypatch.setattr(p, "login", lambda: True)  # the sign-in POST is the one write-shaped call, tested apart
    report = p.run_full(samples=2)

    assert {m for m, _ in sent} == {"GET"}, "nothing but reads after signing in"
    out = report["hub_token_reads"]
    assert list(report)[4] == "hub_token_reads", "the answer heads the report"
    assert (out["identities_checked"], out["enrolled_holders_among_them"]) == (2, 2)
    assert (out["hub_reads"], out["hub_cannot_read"]) == (1, 1)
    assert out["json"] == {"accepted": 1, "unrecognised status": 1}
    assert out["xml"] == {"HTTP 404": 1, "unrecognised status": 1}
    assert out["statuses_seen"] == {"Active": 1, "Activo": 1}
    assert out["count_keys_in_json"] == {"recordsTotal": 1, "none": 1}
    assert report["steps"]["hub_tokens_xml:bad"]["verdict"] == "unrecognised status"
    assert "hub_tokens_json:good" not in report["steps"], "accepted reads are left out"

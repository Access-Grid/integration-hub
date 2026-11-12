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

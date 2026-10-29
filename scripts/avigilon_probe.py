"""Read-only probe of an Avigilon Unity (Plasec) install.

Collects what we need to know before trusting an adapter against a box we
have never seen: which login form it accepts, where its CSRF token lives,
whether the JSON and XML endpoints both answer, what shape each returns, how
token status and dates are written, and which card formats exist.

Three rules, because the output is meant to be pasted into a chat or ticket:

  * **Read-only.** GET requests only, plus the POST that signs in. Nothing
    is created, updated or deleted.
  * **No secrets.** The password, cookie values and CSRF tokens never reach
    the report — cookie *names* do, because which ones appear is the finding.
  * **No contact details or card numbers.** Values under keys that look like
    a name, email, phone, PIN or number are replaced by their shape
    (`<str len=5 digits>`), so the report still shows what kind of value sits
    there. Dates, statuses and flags are kept: their format is the point.

Usage:
    python scripts/avigilon_probe.py <host> <username> [--samples 3]
The password is prompted for. The report is printed and saved as JSON.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import httpx

_PII_KEY = re.compile(r"name|email|phone|pin$|number|photo|address|udf|login", re.I)
_VERSION = re.compile(r"(?:version|ver|v)[\s:=\"']{0,3}(\d+\.\d+(?:\.\d+){0,2})", re.I)
_CSRF_META = re.compile(r"<meta[^>]+csrf-token[^>]*>", re.I)
_PREFIX_SIGNALS = ("Fname", "Lname", "Idstatus", "Tokenstatus", "Internalnumber")

# The XML identity search 500s unless every one of these is present.
_XML_SEARCH = {
    "identity_search_exec_search": "true", "adv_search_field_0": "",
    "adv_search_udf_0": "", "adv_search_val_0": "", "search_pattern_0": "2",
    "adv_search_and_or": "&", "adv_search_cnt": "0", "adv_search_exec_search": "true",
    "quick_search": "true", "qck_search_and_or": "&", "lnam": "", "fnam": "", "tkn": "",
    "search_pattern_fnam": "2", "search_pattern_lnam": "2", "group_id": "", "id": "",
}


def redact(value: Any, key: str = "", pii: bool = True) -> Any:
    """The value with anything personal swapped for a description of it."""
    if isinstance(value, dict):
        return {k: redact(v, k, pii) for k, v in value.items()}
    if isinstance(value, list):
        out = [redact(v, key, pii) for v in value[:3]]
        return out + [f"<{len(value) - 3} more>"] if len(value) > 3 else out
    if not (pii and isinstance(value, str) and value and _PII_KEY.search(key)):
        return value
    if value.lower() == "accessgrid":
        return value  # the enrollment trigger, not a card number
    kind = "digits" if value.isdigit() else "text"
    return f"<str len={len(value)} {kind}>"


def xml_shape(text: str) -> Any:
    """Root tag, row count, and the first row's fields with values redacted."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return {"parse_error": str(e), "head": text[:200]}
    rows = list(root)
    first = {c.tag: redact(c.text or "", c.tag) for c in rows[0].iter() if c is not rows[0]} if rows else {}
    return {"root": root.tag, "rows": len(rows), "row_tag": rows[0].tag if rows else None, "first_row": first}


def flat(element: ET.Element) -> dict[str, str]:
    """An XML row as {tag: text} over its descendants — the adapter's shape."""
    row: dict[str, str] = {}
    for child in element.iter():
        if child is not element:
            row.setdefault(child.tag, (child.text or "").strip())
    return row


class Probe:
    def __init__(self, host: str, username: str, password: str):
        self.base = f"https://{host}"
        self.username, self.password = username, password
        self.http = httpx.Client(base_url=self.base, verify=False, timeout=30.0,
                                 headers={"User-Agent": "AGSyncProbe/1"})
        # `formats` comes first so the answer to "JSON, XML or both?" heads
        # the report: per read, how many times each format answered or why not.
        self.report: dict[str, Any] = {"host": host, "started": datetime.now().isoformat(),
                                       "formats": {}, "steps": {}}

    def get(self, step: str, path: str, **kw: Any) -> httpx.Response | None:
        """GET, recording status, timing, type and the server's self-description."""
        t = time.monotonic()
        try:
            resp = self.http.get(path, **kw)
        except httpx.HTTPError as e:
            self.report["steps"][step] = {"path": path, "error": f"{type(e).__name__}: {e}"}
            return None
        self.report["steps"][step] = {
            "path": path, "status": resp.status_code,
            "seconds": round(time.monotonic() - t, 2),
            "content_type": resp.headers.get("content-type", ""),
            "bytes": len(resp.content),
            "location": resp.headers.get("location", ""),
            "server": resp.headers.get("server", ""),
            "powered_by": resp.headers.get("x-powered-by", ""),
        }
        return resp

    def note(self, step: str, **facts: Any) -> None:
        self.report["steps"].setdefault(step, {}).update(facts)

    def json_body(self, step: str, resp: httpx.Response | None) -> Any:
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            self.note(step, not_json=resp.text[:200])
            return None

    def tally(self, read: str, fmt: str, outcome: str) -> None:
        counts = self.report["formats"].setdefault(read, {}).setdefault(fmt, {})
        counts[outcome] = counts.get(outcome, 0) + 1

    def outcome(self, resp: httpx.Response | None) -> str:
        if resp is None:
            return "no response"
        if resp.status_code == 302 and "/sessions" in resp.headers.get("location", ""):
            return "sent to sign-in"
        return "ok" if resp.status_code == 200 else f"HTTP {resp.status_code}"

    def read_json(self, read: str, step: str, path: str, **kw: Any) -> Any:
        """The parsed body, or None — with the reason tallied either way."""
        resp = self.get(step, path, headers={"Accept": "application/json"}, **kw)
        result = self.outcome(resp)
        body = self.json_body(step, resp) if result == "ok" else None
        if result == "ok" and body is None:
            result = "200 but not JSON"
        self.tally(read, "json", result)
        return body

    def read_xml(self, read: str, step: str, path: str, root_tag: str, row_tag: str, **kw: Any) -> list | None:
        """Rows flattened as the adapter flattens them, or None — reason tallied."""
        resp = self.get(step, path, headers={"Accept": "*/*", "X-Requested-With": "XMLHttpRequest"}, **kw)
        result, rows = self.outcome(resp), None
        if result == "ok":
            self.note(step, shape=xml_shape(resp.text))
            try:
                root = ET.fromstring(resp.text)
            except ET.ParseError:
                root = None
            if root is None:
                result = "200 but not XML"
            elif root.tag == row_tag:
                rows = [flat(root)]
            elif root.tag in (root_tag, "nil-classes"):
                rows = [flat(r) for r in root.findall(row_tag)]
            else:
                result = f"200 but <{root.tag}>, not <{root_tag}>"
        self.tally(read, "xml", result)
        return rows

    def run(self, samples: int) -> dict[str, Any]:
        self.landing()
        if not self.login():
            return self.report
        self.dashboard()
        json_ids = self.identities(samples)
        xml_ids = self.identities_xml(samples)
        self.per_identity(json_ids or xml_ids, samples)
        self.card_formats()
        return self.report

    def landing(self) -> None:
        resp = self.get("login_page", "/sessions/new", follow_redirects=True)
        if resp is not None:
            self.note("login_page", version_hints=sorted(set(_VERSION.findall(resp.text)))[:10],
                      title=(re.search(r"<title>(.*?)</title>", resp.text, re.S | re.I) or [None, ""])[1].strip())

    def login(self) -> bool:
        """Try the form login avigilon-service saw live, then the JSON one ours sends."""
        attempts = {
            "form": {"data": {"login": self.username, "password": self.password}},
            "json": {"json": {"login": self.username, "password": self.password},
                     "headers": {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}},
        }
        results = {}
        for name, kw in attempts.items():
            self.http.cookies.clear()
            try:
                resp = self.http.post("/sessions", follow_redirects=True, **kw)
            except httpx.HTTPError as e:
                results[name] = {"error": f"{type(e).__name__}: {e}"}
                continue
            ok = bool(self.http.cookies.get("_session_id"))
            results[name] = {"status": resp.status_code, "final_path": resp.url.path,
                             "cookie_names": sorted({c.name for c in self.http.cookies.jar}), "ok": ok}
            if ok:
                self.report["steps"]["login"] = {"accepted": name, "attempts": results}
                return True
        self.report["steps"]["login"] = {"accepted": None, "attempts": results}
        return False

    def dashboard(self) -> None:
        resp = self.get("dashboard", "/", follow_redirects=True)
        if resp is None:
            return
        self.note("dashboard",
                  csrf_meta_tag=bool(_CSRF_META.search(resp.text)),
                  csrf_meta_name_first=bool(re.search(r"<meta[^>]+name=[\"']csrf-token[\"'][^>]+content=", resp.text, re.I)),
                  xsrf_cookie=bool(self.http.cookies.get("XSRF-TOKEN")),
                  prefixes_seen=sorted({p for p in ("plasec", "avigilon") for s in _PREFIX_SIGNALS if p + s in resp.text}),
                  version_hints=sorted(set(_VERSION.findall(resp.text)))[:10])

    def identities(self, samples: int) -> list[str]:
        """First page of the JSON list, plus whether paging reports a total."""
        body = self.read_json("identities", "identities_json", "/identities.json",
                              params={"page": 1, "perpage": 25, "sort_by": "plasecName", "order": "ascend"})
        if not isinstance(body, dict):
            return []
        rows = body.get("data") or []
        self.note("identities_json", top_keys=sorted(body), meta=body.get("meta"),
                  rows_on_page=len(rows), sample=redact(rows[:samples]))
        self.report["formats"]["identities"]["json_total"] = (body.get("meta") or {}).get("recordsFiltered")
        # A second page proves whether `page` is honoured or ignored.
        resp2 = self.get("identities_json_page2", "/identities.json",
                         params={"page": 2, "perpage": 25}, headers={"Accept": "application/json"})
        body2 = self.json_body("identities_json_page2", resp2)
        if isinstance(body2, dict):
            ids1 = {str(r.get("cn") or r.get("id")) for r in rows}
            ids2 = {str(r.get("cn") or r.get("id")) for r in body2.get("data") or []}
            self.note("identities_json_page2", rows=len(ids2), overlaps_page1=len(ids1 & ids2))
        return [str(r.get("cn") or r.get("id")) for r in rows if (r.get("cn") or r.get("id")) not in (None, "0")]

    def identities_xml(self, samples: int) -> list[str]:
        """The XML roster. Not paged, so its row count should match the JSON total."""
        rows = self.read_xml("identities", "identities_xml", "/identities.xml",
                             "identities", "identity", params=_XML_SEARCH)
        if rows is None:
            return []
        self.report["formats"]["identities"]["xml_rows"] = len(rows)
        self.note("identities_xml", sample=redact(rows[:samples]))
        return [r["cn"] for r in rows if r.get("cn") not in (None, "", "0")]

    def per_identity(self, ids: list[str], samples: int) -> None:
        """Tokens and detail, each tried as JSON and as XML, for the first few
        identities holding tokens. Stats are kept per format, because the two
        may well write statuses and dates differently."""
        found = []
        stats = {fmt: {"tokens_seen": 0, "accessgrid_tokens": 0, "token_statuses": {}, "date_examples": set()}
                 for fmt in ("json", "xml")}
        for cn in ids[:25]:
            if len(found) >= samples:
                break
            body = self.read_json("tokens", f"tokens_json:{cn}", f"/identities/{cn}/tokens.json")
            json_tokens = (body.get("tokens") or body.get("data") or []) if isinstance(body, dict) else (body or [])
            xml_tokens = self.read_xml("tokens", f"tokens_xml:{cn}", f"/identities/{cn}/tokens.xml",
                                       "tokens", "token") or []
            if not json_tokens and not xml_tokens:
                # Counted in `formats` already; the steps themselves tell us nothing.
                self.report["steps"].pop(f"tokens_json:{cn}", None)
                self.report["steps"].pop(f"tokens_xml:{cn}", None)
                continue
            for fmt, tokens in (("json", json_tokens), ("xml", xml_tokens)):
                for t in tokens:
                    attrs = t.get("attributes", t) if isinstance(t, dict) else {}
                    ext = attrs.get("extended_attributes") or {}
                    s = stats[fmt]
                    status = str(ext.get("token_status", attrs.get("plasecTokenstatus")))
                    s["tokens_seen"] += 1
                    s["token_statuses"][status] = s["token_statuses"].get(status, 0) + 1
                    if str(attrs.get("plasecEmbossednumber", "")).lower() == "accessgrid":
                        s["accessgrid_tokens"] += 1
                    for k, v in {**attrs, **ext}.items():
                        if "date" in k.lower() and v and len(s["date_examples"]) < 8:
                            s["date_examples"].add(f"{k}={v}")
            if json_tokens:
                self.note(f"tokens_json:{cn}", top_keys=sorted(body) if isinstance(body, dict) else "list",
                          sample=redact(json_tokens[:2]))
            if xml_tokens:
                self.note(f"tokens_xml:{cn}", sample=redact(xml_tokens[:2]))
            detail = self.read_json("identity_detail", f"identity_json:{cn}", f"/identities/{cn}.json")
            if detail is not None:
                self.note(f"identity_json:{cn}", sample=redact(detail))
            self.read_xml("identity_detail", f"identity_xml:{cn}", f"/identities/{cn}.xml", "identities", "identity")
            found.append(cn)
        for s in stats.values():
            s["date_examples"] = sorted(s["date_examples"])
        self.report["token_summary"] = stats

    def card_formats(self) -> None:
        body = self.read_json("card_formats", "card_formats", "/card_formats.json")
        self.read_xml("card_formats", "card_formats_xml", "/card_formats.xml", "card-formats", "card-format")
        if body is not None:
            rows = body.get("data", body) if isinstance(body, dict) else body
            self.note("card_formats", count=len(rows) if isinstance(rows, list) else None,
                      formats=redact(rows, pii=False))  # formats hold no personal data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("host", help="appliance address, e.g. 10.0.0.5 (no https://)")
    ap.add_argument("username")
    ap.add_argument("--samples", type=int, default=3, help="identities with tokens to sample")
    args = ap.parse_args()
    password = getpass.getpass("Unity password: ")

    report = Probe(args.host, args.username, password).run(args.samples)
    report["finished"] = datetime.now().isoformat()
    out = f"avigilon-probe-{args.host.replace(':', '_')}-{datetime.now():%Y%m%d-%H%M%S}.json"
    text = json.dumps(report, indent=2, default=str)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nSaved to {out}")
    return 0 if report["steps"].get("login", {}).get("accepted") else 1


if __name__ == "__main__":
    raise SystemExit(main())

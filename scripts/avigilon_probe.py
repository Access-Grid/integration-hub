"""Read-only probe of an Avigilon Unity (Plasec) install.

Collects what we need to know before trusting an adapter against a box we
have never seen: which login form it accepts, where its CSRF token lives,
whether the JSON and XML endpoints both answer, what shape each returns, how
token status and dates are written, and which card formats exist. It also
checks that this machine can reach AccessGrid, which the hub needs as much
as it needs the appliance.

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
    python scripts/avigilon_probe.py [all|xml] <host> <username> [--samples 3]
`all` (the default) reads everything as JSON and as XML. `xml` skips the
JSON list and tests whether `/identities.xml` can be read in pieces — page
and perpage, start and length, the A–Z last-name filter — against the whole
list, read with a long timeout. The password is prompted for. The report is
printed and saved as JSON.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import socket
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import httpx

_PII_KEY = re.compile(r"name|email|phone|pin$|number|photo|address|udf|login", re.I)
_VERSION = re.compile(r"(?:version|ver|v)[\s:=\"']{0,3}(\d+\.\d+(?:\.\d+){0,2})", re.I)
_CSRF_META = re.compile(r"<meta[^>]+csrf-token[^>]*>", re.I)
_PREFIX_SIGNALS = ("Fname", "Lname", "Idstatus", "Tokenstatus", "Internalnumber")
ACCESSGRID_HOST = "api.accessgrid.com"
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "NO_PROXY", "no_proxy")

# The XML identity search 500s unless every one of these is present.
_XML_SEARCH = {
    "identity_search_exec_search": "true", "adv_search_field_0": "",
    "adv_search_udf_0": "", "adv_search_val_0": "", "search_pattern_0": "2",
    "adv_search_and_or": "&", "adv_search_cnt": "0", "adv_search_exec_search": "true",
    "quick_search": "true", "qck_search_and_or": "&", "lnam": "", "fnam": "", "tkn": "",
    "search_pattern_fnam": "2", "search_pattern_lnam": "2", "group_id": "", "id": "",
}

# Ways the XML list might be read in pieces. No code of ours has seen it
# honour any of them; each is checked against the whole list.
_XML_PIECES = {
    "page_1": {"page": "1", "perpage": "25"},
    "page_2": {"page": "2", "perpage": "25"},
    "start_0": {"start": "0", "length": "25"},
    "start_25": {"start": "25", "length": "25"},
    "letter_a": {"letter_search": "true", "lnam": "A"},
}
_PIECE_SIZE = 25
_WHOLE_LIST_TIMEOUT = 300.0  # unpaged; a large site took over 30s
_PIECE_TIMEOUT = 120.0
MODES = ("all", "xml")

# What our adapter reads and writes (src/agsync/lib/pacs/avigilon/). Copied
# rather than imported so the exe stays standalone; tests/test_avigilon_probe.py
# fails if these drift from the client's. Field names are suffixes after the
# `plasec` (or `avigilon`) prefix, as the client looks them up.
_PREFIXES = ("plasec", "avigilon")
IDENTITY_FIELDS = ("Fname", "Lname", "Name", "Idstatus", "identityEmailaddress", "identityPhone",
                   "identityWorkphone", "identityTitle", "identityDepartment")
# Token status and dates may instead sit under extended_attributes.
TOKEN_FIELDS = {"Internalnumber": None, "Embossednumber": None, "Tokenstatus": "token_status",
                "Issuedate": "formatted_issue_date", "Activatedate": "formatted_activate_date",
                "Deactivatedate": "formatted_deactivate_date"}
TOKEN_FORM_FIELDS = [
    "plasecInternalnumber", "plasecEmbossednumber", "plasecPIN", "plasecTokenType",
    "plasecTokenlevel", "plasecTokenMobileAppType", "plasecTokenOrigoMobileIdType",
    "plasecDownload", "plasecTokenEnableReValidation", "plasecTokenUnitofUpdatePeriod",
    "plasecTokenVIP", "plasecTrace", "plasectokenExtaccess", "plasecTokennoexpire",
    "plasectokenPinexempt", "plasectokenUseloseexempt", "plasecTokenOffice",
    "plasecTokenSetLockDown", "plasecTokenAuditOpenings", "plasecTokenOverridePrivacy",
    "plasecTokenOverrideLockDown",
]
TOKEN_FORM_DATES = ["plasecIssuedate", "plasecActivatedate", "plasecDeactivatedate"]
STATUS_CODES = {"active": "1", "inactive": "2", "not yet active": "3", "expired": "4"}
DATE_FORMATS = ("%Y%m%d%H%M%SZ", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y")


def adapter_reads_date(text: str) -> bool:
    """Whether the adapter's parse_date would accept this text."""
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(text, fmt)
            return True
        except ValueError:
            continue
    return False


def _name_for(suffix: str, alt: str | None, names: set[str], ext_names: set[str]) -> str | None:
    """The field name Unity used for what the adapter looks up as `suffix`, or None."""
    hit = next((p + suffix for p in _PREFIXES if p + suffix in names), None)
    if hit is None and alt and (alt in ext_names or alt in names):
        hit = alt
    return hit


def attrs_of(raw: Any) -> dict[str, Any]:
    """The fields of a row in either shape: flat, JSON:API, or wrapped in `data`."""
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        raw = raw["data"]
    if not isinstance(raw, dict):
        return {}
    return raw["attributes"] if isinstance(raw.get("attributes"), dict) else raw


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
    def __init__(self, host: str, username: str, password: str, mode: str = "all"):
        self.base = f"https://{host}"
        self.username, self.password = username, password
        self.http = httpx.Client(base_url=self.base, verify=False, timeout=30.0,
                                 headers={"User-Agent": "AGSyncProbe/1"})
        # `formats` comes first so the answer to "JSON, XML or both?" heads
        # the report: per read, how many times each format answered or why not.
        self.report: dict[str, Any] = {"host": host, "mode": mode, "started": datetime.now().isoformat(),
                                       "accessgrid": {}, "formats": {}, "fields": {}, "steps": {}}
        # Rows as read, per source ("token xml", "identity list json", ...), for
        # comparing their field names and values against what the adapter expects.
        self.rows: dict[str, list[dict[str, Any]]] = {}
        if mode == "xml":
            self.report = {**dict(list(self.report.items())[:4]), "xml_paging": {}, **self.report}

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
        # A short refusal (a 406, say) usually says why in its body; a long
        # one is an HTML page that could hold names, so it is left out.
        if resp.status_code >= 400 and len(resp.content) <= 300:
            self.report["steps"][step]["body"] = resp.text
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

    def accessgrid(self) -> None:
        """Can this machine reach AccessGrid, as the hub will need to?

        Not an ICMP ping: that is often blocked on networks where HTTPS works,
        and HTTPS is what the hub uses. Checked in layers so a failure names
        the layer — name lookup, a direct TCP connection, then HTTPS with the
        certificate verified, where a proxy that re-signs traffic shows up.
        Every layer runs even if one before it failed: a site that must go
        through a proxy fails the first two and still passes the third.
        """
        result = self.report["accessgrid"]
        result["host"] = ACCESSGRID_HOST
        # Names only: a proxy URL can carry a username and password.
        result["proxy_env_vars"] = [k for k in _PROXY_VARS if os.environ.get(k)]
        try:
            result["dns"] = sorted({i[4][0] for i in socket.getaddrinfo(ACCESSGRID_HOST, 443, type=socket.SOCK_STREAM)})
        except OSError as e:
            result["dns"] = f"failed: {e}"
        t = time.monotonic()
        try:
            with socket.create_connection((ACCESSGRID_HOST, 443), timeout=10):
                result["tcp_443"] = f"ok in {time.monotonic() - t:.2f}s"
        except OSError as e:
            result["tcp_443"] = f"failed: {type(e).__name__}: {e}"
        t = time.monotonic()
        try:
            resp = httpx.get(f"https://{ACCESSGRID_HOST}/", timeout=15, headers={"User-Agent": "AGSyncProbe/1"})
            # Any status at all means we reached AccessGrid and trusted its certificate.
            result["https"] = f"ok: HTTP {resp.status_code} in {time.monotonic() - t:.2f}s"
        except httpx.HTTPError as e:
            result["https"] = f"failed: {type(e).__name__}: {e}"
        result["reachable"] = result["https"].startswith("ok")

    def run(self, samples: int) -> dict[str, Any]:
        self.accessgrid()
        self.landing()
        if not self.login():
            return self.report
        self.dashboard()
        json_ids = self.identities(samples)
        xml_ids = self.identities_xml(samples)
        self.per_identity(json_ids or xml_ids, samples)
        self.card_formats()
        self.compare_fields()
        return self.report

    def run_xml(self, samples: int) -> dict[str, Any]:
        self.accessgrid()
        self.landing()
        if not self.login():
            return self.report
        self.dashboard()
        self.per_identity(self.xml_paging(samples), samples)
        self.card_formats()
        self.compare_fields()
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
        self.rows.setdefault("identity list json", []).extend(attrs_of(r) for r in rows)
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

    def identities_xml(self, samples: int, timeout: float = 30.0) -> list[str]:
        """The XML roster. Not paged, so its row count should match the JSON total."""
        rows = self.read_xml("identities", "identities_xml", "/identities.xml",
                             "identities", "identity", params=_XML_SEARCH, timeout=timeout)
        if rows is None:
            return []
        self.report["formats"]["identities"]["xml_rows"] = len(rows)
        self.note("identities_xml", sample=redact(rows[:samples]))
        self.rows.setdefault("identity list xml", []).extend(rows)
        return [r["cn"] for r in rows if r.get("cn") not in (None, "", "0")]

    def xml_paging(self, samples: int) -> list[str]:
        """Can /identities.xml be read in pieces? The pieces go first, with
        their own timeout, so they are answered even if the whole list then
        times out; the whole list is the yardstick each is judged against."""
        pieces: dict[str, set[str]] = {}
        for name, extra in _XML_PIECES.items():
            rows = self.read_xml("identities_xml_pieces", f"xml_{name}", "/identities.xml", "identities",
                                 "identity", params={**_XML_SEARCH, **extra}, timeout=_PIECE_TIMEOUT)
            if rows is not None:
                pieces[name] = {r.get("cn", "") for r in rows} - {"", "0"}
                self.note(f"xml_{name}", rows=len(rows))
        ids = self.identities_xml(samples, timeout=_WHOLE_LIST_TIMEOUT)
        whole = set(ids) if self.report["steps"]["identities_xml"].get("status") == 200 else None

        def paged(first: str, second: str) -> str:
            if first not in pieces or second not in pieces:
                return "not answered"
            a, b = pieces[first], pieces[second]
            if whole is not None and len(whole) <= _PIECE_SIZE:
                return f"can't tell: only {len(whole)} identities"
            if len(a) > _PIECE_SIZE:
                return f"ignored: {len(a)} rows for {_PIECE_SIZE} asked"
            if a == b:
                return "ignored: both pieces hold the same rows"
            if a & b:
                return f"broken: pieces share {len(a & b)} rows"
            if whole is not None and not (a | b) <= whole:
                return "broken: pieces hold rows the whole list lacks"
            return "works"

        letter = pieces.get("letter_a")
        self.report["xml_paging"] = {
            "whole_list": {k: self.report["steps"]["identities_xml"].get(k) for k in ("status", "seconds", "bytes", "error")}
                          | {"identities": None if whole is None else len(whole)},
            "page_perpage": paged("page_1", "page_2"),
            "start_length": paged("start_0", "start_25"),
            "letter_a": "not answered" if letter is None else {
                "identities": len(letter),
                "seconds": self.report["steps"]["xml_letter_a"].get("seconds"),
                "within_whole_list": None if whole is None else letter <= whole,
                "smaller_than_whole_list": None if whole is None else len(letter) < len(whole),
            },
        }
        return ids or sorted(set().union(*pieces.values()))

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
            self.rows.setdefault("token json", []).extend(attrs_of(t) for t in json_tokens)
            self.rows.setdefault("token xml", []).extend(xml_tokens)
            if json_tokens:
                self.note(f"tokens_json:{cn}", top_keys=sorted(body) if isinstance(body, dict) else "list",
                          sample=redact(json_tokens[:2]))
            if xml_tokens:
                self.note(f"tokens_xml:{cn}", sample=redact(xml_tokens[:2]))
            detail = self.read_json("identity_detail", f"identity_json:{cn}", f"/identities/{cn}.json")
            if detail is not None:
                self.note(f"identity_json:{cn}", sample=redact(detail))
                self.rows.setdefault("identity detail json", []).append(attrs_of(detail))
            detail_xml = self.read_xml("identity_detail", f"identity_xml:{cn}", f"/identities/{cn}.xml",
                                       "identities", "identity")
            self.rows.setdefault("identity detail xml", []).extend(detail_xml or [])
            found.append(cn)
        for s in stats.values():
            s["date_examples"] = sorted(s["date_examples"])
        self.report["token_summary"] = stats

    def compare_fields(self) -> None:
        """Per source, which of the adapter's field names Unity actually sent,
        whether the token form could be built, and whether the statuses and
        dates are ones the adapter reads. Names, statuses and dates only —
        none of them identify a person."""
        for source, rows in self.rows.items():
            if not rows:
                continue
            names = set().union(*(r.keys() for r in rows))
            ext_names = set().union(*((r.get("extended_attributes") or {}).keys()
                                      for r in rows if isinstance(r.get("extended_attributes"), dict)))
            found: dict[str, Any] = {}
            token = source.startswith("token")
            wanted = TOKEN_FIELDS if token else dict.fromkeys(IDENTITY_FIELDS)
            found["rows"] = len(rows)
            found["adapter_reads"] = {s: _name_for(s, alt, names, ext_names) for s, alt in wanted.items()}
            known = {v for v in found["adapter_reads"].values() if v}
            if token:
                form = TOKEN_FORM_FIELDS + TOKEN_FORM_DATES
                found["write_form_missing"] = [f for f in form if f not in names]
                known |= set(form)
            # Names the adapter never looks at: a near-miss spelling shows up here.
            found["other_names"] = sorted(names - known - {"extended_attributes"} | ext_names - known)

            status_key = "Tokenstatus" if token else "Idstatus"
            statuses: dict[str, int] = {}
            dates = {"read": 0, "unreadable": []}
            for r in rows:
                ext = r.get("extended_attributes") if isinstance(r.get("extended_attributes"), dict) else {}
                raw = ext.get("token_status") if token and ext.get("token_status") else \
                    next((r[p + status_key] for p in _PREFIXES if r.get(p + status_key) not in (None, "")), "")
                text = str(raw).strip()
                label = f"{text!r} -> {'recognised' if text in ('1', '2', '3', '4') or text.lower() in STATUS_CODES else 'NOT RECOGNISED'}"
                statuses[label] = statuses.get(label, 0) + 1
                if token:
                    for s in ("Issuedate", "Activatedate", "Deactivatedate"):
                        v = str(ext.get(TOKEN_FIELDS[s]) or next((r[p + s] for p in _PREFIXES if r.get(p + s)), "")).strip()
                        if not v:
                            continue
                        if adapter_reads_date(v):
                            dates["read"] += 1
                        elif v not in dates["unreadable"] and len(dates["unreadable"]) < 8:
                            dates["unreadable"].append(v)
            found["statuses"] = statuses
            if token:
                found["dates"] = dates
            self.report["fields"][source] = found

    def card_formats(self) -> None:
        body = self.read_json("card_formats", "card_formats", "/card_formats.json")
        self.read_xml("card_formats", "card_formats_xml", "/card_formats.xml", "card-formats", "card-format")
        if body is not None:
            rows = body.get("data", body) if isinstance(body, dict) else body
            self.note("card_formats", count=len(rows) if isinstance(rows, list) else None,
                      formats=redact(rows, pii=False))  # formats hold no personal data


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] not in MODES and not argv[0].startswith("-"):
        argv.insert(0, "all")  # the form before modes existed: <host> <username>
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=MODES, help="all: JSON and XML; xml: test paging the XML identity list")
    ap.add_argument("host", help="appliance address, e.g. 10.0.0.5 (no https://)")
    ap.add_argument("username")
    ap.add_argument("--samples", type=int, default=3, help="identities with tokens to sample")
    args = ap.parse_args(argv)
    password = getpass.getpass("Unity password: ")

    probe = Probe(args.host, args.username, password, args.mode)
    report = probe.run_xml(args.samples) if args.mode == "xml" else probe.run(args.samples)
    report["finished"] = datetime.now().isoformat()
    out = f"avigilon-probe-{args.mode}-{args.host.replace(':', '_')}-{datetime.now():%Y%m%d-%H%M%S}.json"
    text = json.dumps(report, indent=2, default=str)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nSaved to {out}")
    return 0 if report["steps"].get("login", {}).get("accepted") else 1


if __name__ == "__main__":
    raise SystemExit(main())

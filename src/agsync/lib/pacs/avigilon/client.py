"""HTTP client for Avigilon Unity (Plasec), read side.

JSON first, XML as the fallback. The JSON endpoints are the ones the
standalone avigilon-service agent confirmed against a live server; the XML
ones are what the first port used, and we have not seen enough installs to
drop either. Whichever answers for a read is tried first from then on.

Every response is normalized into plain dicts here so the adapter never sees
the three shapes Unity answers in: a flat JSON row (`{"cn": ...,
"plasecFname": ...}`), a JSON:API one (`{"id": ..., "attributes": {...}}`),
and an XML element, which is flattened into the first.

One rule above all: **a read that fails raises.** Returning `[]` from a
failed read is a claim — "this person holds no tokens" — and the engine acts
on claims by revoking passes. Better a cycle that stops than one that
deletes.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0
HTTP_USER_AGENT = "AGSyncTool/0.1"
PAGE_SIZE = 100
# Ten thousand pages of a hundred is far past any real roster; reaching it
# means the paging is broken, and stopping is better than spinning forever.
MAX_PAGES = 10_000

# Live servers prefix fields `plasec`; the first port also saw `avigilon` on
# some older deployments. Neither has been confirmed against the other.
_PREFIXES = ("plasec", "avigilon")

# What Unity's token edit form posts, as the avigilon-service agent captured
# it. Spelling and case are Unity's own (`plasectokenExtaccess` really is
# lower-case t). Dates sit outside the `token[...]` group.
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

# The XML identity search 500s unless every one of these is present. It is
# not paged: one response carries the whole roster.
XML_IDENTITY_SEARCH = {
    "identity_search_exec_search": "true", "adv_search_field_0": "",
    "adv_search_udf_0": "", "adv_search_val_0": "", "search_pattern_0": "2",
    "adv_search_and_or": "&", "adv_search_cnt": "0", "adv_search_exec_search": "true",
    "quick_search": "true", "qck_search_and_or": "&", "lnam": "", "fnam": "", "tkn": "",
    "search_pattern_fnam": "2", "search_pattern_lnam": "2", "group_id": "", "id": "",
}

# Unity writes statuses as words in some responses and digits in others.
_STATUS_CODES = {"active": "1", "inactive": "2", "not yet active": "3", "expired": "4"}


class UnityError(RuntimeError):
    """Unity could not be read, or answered with something we can't trust."""


class UnityAuthError(UnityError):
    """Unity would not give us a working session."""


def _unwrap(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(id, fields) from either the flat or the JSON:API shape."""
    if isinstance(raw.get("attributes"), dict):
        attrs = raw["attributes"]
        return str(raw.get("id") or attrs.get("cn") or ""), attrs
    return str(raw.get("cn") or raw.get("id") or ""), raw


def xml_rows(text: str, root_tag: str, row_tag: str) -> list[dict[str, str]]:
    """Each `<row_tag>` element as a flat dict of its descendants' text.

    Flat is the shape identity_row/token_row already read, so XML needs no
    parser of its own past this. Anything that is not the expected document
    — a login page, an error page — raises rather than reading as no rows.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise UnityError(f"not XML: {e}") from e
    if root.tag == row_tag:
        elements = [root]
    elif root.tag == "nil-classes" and len(root) == 0:
        elements = []  # how Rails renders an empty array as XML
    elif root.tag == root_tag:
        elements = root.findall(row_tag)
    else:
        raise UnityError(f"expected <{root_tag}>, got <{root.tag}>")
    rows = []
    for element in elements:
        row: dict[str, str] = {}
        for child in element.iter():
            if child is not element:
                row.setdefault(child.tag, (child.text or "").strip())
        rows.append(row)
    return rows


def _field(attrs: dict[str, Any], suffix: str) -> str:
    for prefix in _PREFIXES:
        value = attrs.get(f"{prefix}{suffix}")
        if value not in (None, ""):
            return str(value)
    return ""


def status_code(raw: Any) -> str:
    """'1'..'4' for a recognised status, '' for anything else.

    Deliberately not defaulting to active: what an unknown status should mean
    differs between an identity and a token, so the adapter decides.
    """
    text = str(raw if raw is not None else "").strip()
    if text in ("1", "2", "3", "4"):
        return text
    return _STATUS_CODES.get(text.lower(), "")


def identity_row(raw: dict[str, Any]) -> dict[str, str]:
    identity_id, attrs = _unwrap(raw)
    first, last = _field(attrs, "Fname"), _field(attrs, "Lname")
    listed = _field(attrs, "Name")  # "Last, First" — all the list endpoint carries
    if not first and not last and listed:
        last, _, first = (p.strip() for p in listed.partition(","))
    raw_status = _field(attrs, "Idstatus") or str(attrs.get("status") or "")
    status = status_code(raw_status)
    if raw_status and not status:
        logger.warning("Unity identity %s has an unrecognised status %r", identity_id, raw_status)
    return {
        "id": identity_id,
        "first_name": first,
        "last_name": last,
        "full_name": f"{first} {last}".strip() or listed,
        "email": _field(attrs, "identityEmailaddress"),
        "phone": _field(attrs, "identityPhone"),
        "work_phone": _field(attrs, "identityWorkphone"),
        "title": _field(attrs, "identityTitle"),
        "department": _field(attrs, "identityDepartment"),
        "status": status,
    }


def token_row(raw: dict[str, Any], identity_id: str) -> dict[str, Any]:
    """A token, from the tokens list or embedded in an identity's detail.

    The list puts status (as a word) and dates under `extended_attributes`;
    the detail puts a numeric status and the dates on the token itself. The
    full field set is kept under `fields` because a token update must send
    every field back, and only the original response has them.
    """
    token_id, attrs = _unwrap(raw)
    ext = attrs.get("extended_attributes") or {}
    raw_status = ext.get("token_status") or _field(attrs, "Tokenstatus")
    status = status_code(raw_status)
    if not status:
        logger.warning("Unity token %s has an unrecognised status %r", token_id, raw_status)

    def date(name: str) -> str:
        return str(ext.get(f"formatted_{name}_date") or _field(attrs, f"{name.capitalize()}date"))

    return {
        "id": token_id,
        "identity_id": identity_id,
        "internal_number": _field(attrs, "Internalnumber"),
        "embossed_number": _field(attrs, "Embossednumber"),
        "status": status,
        "issue_date": date("issue"),
        "activate_date": date("activate"),
        "deactivate_date": date("deactivate"),
        "fields": attrs,
    }


class UnityClient:
    """Session-based client. Signs in with a password, so an expired session
    is renewed here rather than reported to a human."""

    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False):
        self.base_url = f"https://{host}"
        self.username = username
        self.password = password
        self._client = httpx.Client(
            base_url=self.base_url,
            verify=verify_ssl,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": HTTP_USER_AGENT},
            follow_redirects=False,
        )
        self._logged_in = False
        # Reads ("identities", "tokens") where XML answered after JSON failed.
        self._xml_first: set[str] = set()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> UnityClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- session -----------------------------------------------------------

    def login(self) -> None:
        """Sign in, and prove it with a read before believing it.

        A `_session_id` cookie is not proof: Rails hands one to anonymous
        visitors too. The form login is what a live server accepted; the
        JSON one is what the first port sent, kept as a fallback until a
        probe of a real box says which to drop.
        """
        self._logged_in = False
        attempts = (
            {"data": {"login": self.username, "password": self.password}},
            {"json": {"login": self.username, "password": self.password},
             "headers": {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}},
        )
        for kwargs in attempts:
            self._client.cookies.clear()
            try:
                self._client.post("/sessions", follow_redirects=True, **kwargs)
                signed_in = self._can_read()
            except httpx.HTTPError as e:
                raise UnityError(f"Unity unreachable: {type(e).__name__}: {e}") from e
            if signed_in:
                self._logged_in = True
                return
        raise UnityAuthError("Unity rejected the username or password")

    def _can_read(self) -> bool:
        """Does the identity list answer with data, as JSON or else as XML?

        Data, not just a 200: a server that renders its login page with a
        200 instead of redirecting would pass a status check alone. And only
        being sent back to sign in counts as a rejected login — a box whose
        identity list errors raises, so a 500 is never reported to the
        operator as a wrong password.
        """
        probes = (
            ("/identities.json", {"page": 1, "perpage": 1}, "json"),
            ("/identities.xml", XML_IDENTITY_SEARCH, "xml"),
        )
        answers = []
        for path, params, kind in probes:
            resp = self._client.get(path, params=params)
            if self._is_session_expired(resp):
                return False
            if resp.status_code == 200 and kind in resp.headers.get("content-type", ""):
                return True
            answers.append((path, resp.status_code))
        if any(status == 200 for _, status in answers):
            return False  # a page instead of data: the sign-in page
        raise UnityError("signed in, but the identity list would not answer: "
                         + "; ".join(f"{p} HTTP {s}" for p, s in answers))

    @property
    def csrf_token(self) -> str:
        return self._client.cookies.get("XSRF-TOKEN") or ""

    @staticmethod
    def _is_session_expired(resp: httpx.Response) -> bool:
        if resp.status_code == 401:
            return True
        return resp.status_code == 302 and "/sessions" in resp.headers.get("Location", "")

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send once; if the session has lapsed, sign in again and resend once."""
        if not self._logged_in:
            self.login()
        for attempt in (1, 2):
            headers = dict(kwargs.pop("headers", None) or {})
            if method != "GET":
                headers["X-CSRF-Token"] = self.csrf_token
            try:
                resp = self._client.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as e:
                raise UnityError(f"{method} {path}: {type(e).__name__}: {e}") from e
            kwargs["headers"] = headers
            if not self._is_session_expired(resp):
                return resp
            if attempt == 1:
                logger.info("Unity session expired — signing in again")
                self.login()
        raise UnityAuthError(f"{method} {path}: session expired again straight after signing in")

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._request("GET", path, params=params, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise UnityError(f"GET {path}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise UnityError(f"GET {path}: response is not JSON") from e

    # -- reads -------------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.login()
        except UnityError as e:
            return False, str(e)
        return True, ""

    def _json_then_xml(self, what: str, from_json: Callable[[], Any], from_xml: Callable[[], Any]) -> Any:
        """Read one way, and if that fails, the other.

        Safe because neither way can return part of an answer — each is
        whole or raises — so the fallback can only add a complete read, never
        dress up a broken one. A failed sign-in is not retried as XML: the
        format was never the problem.
        """
        xml_first = what in self._xml_first
        reads = [("XML", from_xml), ("JSON", from_json)] if xml_first else [("JSON", from_json), ("XML", from_xml)]
        errors = []
        for name, read in reads:
            try:
                result = read()
            except UnityAuthError:
                raise
            except UnityError as e:
                errors.append(f"{name}: {e}")
                continue
            if (name == "XML") != xml_first:
                logger.warning("Unity %s: reading as %s from now on (%s)", what, name, "; ".join(errors))
                self._xml_first ^= {what}
            return result
        raise UnityError(f"{what}: " + "; ".join(errors))

    def list_identities(self) -> list[dict[str, str]]:
        """Every identity, or an exception. Never a partial roster."""
        rows = self._json_then_xml("identities", self._identities_json, self._identities_xml)
        logger.info("Unity: %d identities", len(rows))
        return rows

    def _identities_xml(self) -> list[dict[str, str]]:
        resp = self._request("GET", "/identities.xml", params=XML_IDENTITY_SEARCH,
                             headers={"X-Requested-With": "XMLHttpRequest", "Accept": "*/*"})
        if resp.status_code != 200:
            raise UnityError(f"GET /identities.xml: HTTP {resp.status_code}")
        rows = [identity_row(r) for r in xml_rows(resp.text, "identities", "identity")]
        return [r for r in rows if r["id"]]

    def _identities_json(self) -> list[dict[str, str]]:
        """Page through the JSON list.

        A short roster reads to the engine as people removed from Unity, so
        a page that fails, a server that ignores `page`, or a total we never
        reach all stop the read instead of returning what we have.
        """
        rows: dict[str, dict[str, str]] = {}
        for page in range(1, MAX_PAGES + 1):
            body = self._get_json("/identities.json", {"page": page, "perpage": PAGE_SIZE})
            items = body.get("data") if isinstance(body, dict) else None
            if not isinstance(items, list):
                raise UnityError(f"identities page {page}: no data list in the response")
            meta = body.get("meta") or {}
            total = meta.get("recordsFiltered") or meta.get("recordsTotal")
            new = [r for r in map(identity_row, items) if r["id"] and r["id"] not in rows]
            if items and not new:
                raise UnityError(f"identities page {page} repeated earlier pages — paging ignored")
            rows.update((r["id"], r) for r in new)
            if not items or (total is not None and len(rows) >= int(total)):
                break
        else:
            raise UnityError(f"identities: still paging after {MAX_PAGES} pages")
        if total is not None and len(rows) < int(total):
            raise UnityError(f"identities: read {len(rows)} of {total}")
        return list(rows.values())

    def get_identity(self, identity_id: str) -> dict[str, str]:
        """Full detail — the list has no email, phone, title or department."""
        body = self._get_json(f"/identities/{identity_id}.json")
        raw = body.get("data", body) if isinstance(body, dict) else None
        if not isinstance(raw, dict):
            raise UnityError(f"identity {identity_id}: unexpected response shape")
        return identity_row(raw)

    def list_tokens(self, identity_id: str) -> list[dict[str, Any]]:
        return self._json_then_xml(
            "tokens",
            lambda: self._tokens_json(identity_id),
            lambda: self._tokens_xml(identity_id),
        )

    def _tokens_xml(self, identity_id: str) -> list[dict[str, Any]]:
        path = f"/identities/{identity_id}/tokens.xml"
        resp = self._request("GET", path, headers={"Accept": "*/*"})
        if resp.status_code != 200:
            raise UnityError(f"GET {path}: HTTP {resp.status_code}")
        # A person with no tokens may come back as an empty <tokens/>; that
        # parses to no rows, which here is the truth rather than a failure.
        return [token_row(t, identity_id) for t in xml_rows(resp.text, "tokens", "token")]

    def _tokens_json(self, identity_id: str) -> list[dict[str, Any]]:
        body = self._get_json(f"/identities/{identity_id}/tokens.json")
        items = body.get("tokens", body.get("data")) if isinstance(body, dict) else body
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            raise UnityError(f"tokens for {identity_id}: unexpected response shape")
        return [token_row(t, identity_id) for t in items if isinstance(t, dict)]

    # -- writes ------------------------------------------------------------

    def set_token_status(self, token: dict[str, Any], status: str) -> None:
        """Change one token's status, sending every other field back unchanged.

        Unity's token update is a full form post: a field left out is reset.
        So the form is built only from what Unity itself returned for this
        token, and if any field the form needs is missing from that, nothing
        is sent — inventing a value would overwrite whatever the operator set.
        Raises on refusal or failure; returns only once a re-read shows the
        new status.
        """
        attrs = token["fields"]
        missing = [f for f in TOKEN_FORM_FIELDS + TOKEN_FORM_DATES if f not in attrs]
        if missing:
            raise UnityError(f"token {token['id']}: Unity did not return {', '.join(missing)}")
        form = {
            "utf8": "✓",
            "_method": "put",
            "authenticity_token": self.csrf_token,
            "enrollVirdiAfter": "false",
            **{f"token[{f}]": str(attrs[f] if attrs[f] is not None else "") for f in TOKEN_FORM_FIELDS},
            **{f: str(attrs[f] if attrs[f] is not None else "") for f in TOKEN_FORM_DATES},
            "token[plasecTokenstatus]": status,
        }
        path = f"/identities/{token['identity_id']}/tokens/{token['id']}"
        resp = self._request("POST", path, data=form)
        if resp.status_code != 302:
            raise UnityError(f"POST {path}: HTTP {resp.status_code}")
        after = next((t for t in self.list_tokens(token["identity_id"]) if t["id"] == token["id"]), None)
        if after is None or after["status"] != status:
            raise UnityError(f"token {token['id']}: status reads {after and after['status']!r} after writing {status!r}")

    def list_card_formats(self) -> list[dict[str, str]]:
        body = self._get_json("/card_formats.json")
        items = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(items, list):
            raise UnityError("card formats: unexpected response shape")
        formats = []
        for raw in items:
            format_id, attrs = _unwrap(raw)
            formats.append({
                "id": format_id,
                "name": str(attrs.get("plasecName") or ""),
                "facility_code": str(attrs.get("plaseccfmtFacilitycode") or ""),
                "total_bits": str(attrs.get("plaseccfmtMaxdigits") or ""),
            })
        return formats

"""HTTP client for Avigilon Unity.

Ported from the avigilon-unity-chrome-plugin bridge. Behaviorally
identical: handles JSON and XML responses, scrapes CSRF from a meta tag
in the dashboard HTML, auto-detects the `plasec` vs `avigilon` field
prefix some legacy deployments expose, and bypasses SSL verification by
default for self-signed appliance certs.

Switched from `requests` to `httpx` for project consistency.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0
HTTP_USER_AGENT = "AGSyncTool/0.1"

AVIGILON_TOKEN_TYPE_STANDARD = "0"
AVIGILON_TOKEN_STATUS_ACTIVE = "1"


class AvigilonAuthError(Exception):
    pass


class AvigilonAPIError(Exception):
    pass


class AvigilonClient:
    """Session-based HTTP client for Avigilon Unity."""

    _XML_PREFIXES = ("avigilon", "plasec")

    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False):
        self.base_url = f"https://{host}"
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl

        self._client = httpx.Client(
            base_url=self.base_url,
            verify=verify_ssl,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": HTTP_USER_AGENT},
            follow_redirects=False,
        )
        self._logged_in = False
        self._csrf_meta_token = ""
        self._prefix = "avigilon"

        logger.info(
            "AvigilonClient init: base_url=%s username=%r verify_ssl=%s",
            self.base_url, self.username, verify_ssl,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AvigilonClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @property
    def csrf_token(self) -> str:
        return self._csrf_meta_token or self._client.cookies.get("XSRF-TOKEN", "") or ""

    @staticmethod
    def _extract_csrf_meta(html: str) -> str:
        m = re.search(
            r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
            html or "", re.IGNORECASE,
        )
        return m.group(1) if m else ""

    def _detect_prefix_from_text(self, text: str) -> None:
        if not text:
            return
        signals = ("Fname", "Lname", "Idstatus", "identityEmailaddress")
        for prefix in ("plasec", "avigilon"):
            for sig in signals:
                if f"{prefix}{sig}" in text:
                    if self._prefix != prefix:
                        logger.info("Detected Avigilon field prefix: %r", prefix)
                    self._prefix = prefix
                    return

    def login(self) -> bool:
        try:
            resp = self._client.post(
                "/sessions",
                json={"login": self.username, "password": self.password},
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": self.base_url,
                    "Referer": f"{self.base_url}/sessions/new",
                },
            )
            logger.info("Avigilon login: status=%s elapsed=%.2fs", resp.status_code, resp.elapsed.total_seconds())

            if resp.status_code == 200 and self._client.cookies.get("_session_id"):
                self._logged_in = True
                self._detect_prefix_from_text(resp.text)
                self._fetch_dashboard_state()
                logger.info(
                    "Avigilon login SUCCESS: csrf_meta=%s prefix=%r",
                    "set" if self._csrf_meta_token else "missing",
                    self._prefix,
                )
                return True

            logger.error("Avigilon login FAILED: status=%s", resp.status_code)
            return False
        except httpx.HTTPError as e:
            logger.error("Avigilon login transport error: %s: %s", type(e).__name__, e)
            return False

    def _fetch_dashboard_state(self) -> None:
        try:
            resp = self._client.get(
                "/", headers={"Accept": "text/html,application/xhtml+xml"},
                follow_redirects=True,
            )
            csrf = self._extract_csrf_meta(resp.text)
            if csrf:
                self._csrf_meta_token = csrf
            self._detect_prefix_from_text(resp.text)
        except httpx.HTTPError as e:
            logger.warning("Dashboard state fetch failed: %s", e)

    def _ensure_authenticated(self) -> None:
        if not self._logged_in and not self.login():
            raise AvigilonAuthError("Cannot authenticate with Avigilon server")

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._ensure_authenticated()
        headers = kwargs.pop("headers", {})
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            headers.setdefault("X-CSRF-Token", self.csrf_token)
        resp = self._client.request(method, path, headers=headers, **kwargs)
        if self._is_session_expired(resp, path):
            logger.warning("Avigilon session expired — re-authenticating")
            self._logged_in = False
            if self.login():
                headers["X-CSRF-Token"] = self.csrf_token
                resp = self._client.request(method, path, headers=headers, **kwargs)
        return resp

    @staticmethod
    def _is_session_expired(resp: httpx.Response, requested_path: str) -> bool:
        if resp.status_code == 302:
            return "/sessions" in resp.headers.get("Location", "")
        if resp.status_code == 200 and requested_path != "/sessions":
            return "/sessions" in str(resp.url)
        return False

    # ------------------------------------------------------------------
    # Identity / token operations
    # ------------------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        try:
            if not self._logged_in and not self.login():
                return False, "Login failed — check host/credentials"
            resp = self._request("GET", "/identities.xml", params=self._identities_search_params())
            if resp.status_code == 200:
                return True, ""
            return False, f"Probe returned HTTP {resp.status_code}"
        except (AvigilonAuthError, httpx.HTTPError) as e:
            return False, f"{type(e).__name__}: {e}"

    @staticmethod
    def _identities_search_params() -> dict[str, str]:
        # The XML endpoint 500s if any adv_search_* field is missing.
        return {
            "identity_search_exec_search": "true",
            "adv_search_field_0": "",
            "adv_search_udf_0": "",
            "adv_search_val_0": "",
            "search_pattern_0": "2",
            "adv_search_and_or": "&",
            "adv_search_cnt": "0",
            "adv_search_exec_search": "true",
            "quick_search": "true",
            "qck_search_and_or": "&",
            "lnam": "",
            "fnam": "",
            "tkn": "",
            "search_pattern_fnam": "2",
            "search_pattern_lnam": "2",
            "group_id": "",
            "id": "",
        }

    def get_all_identities(self) -> list[dict[str, Any]]:
        resp = self._request(
            "GET", "/identities.xml",
            params=self._identities_search_params(),
            headers={
                "X-CSRF-Token": self.csrf_token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "*/*",
                "Referer": f"{self.base_url}/identities",
            },
        )
        if resp.status_code != 200:
            logger.error("get_all_identities: HTTP %s", resp.status_code)
            return []
        try:
            return self._parse_identities_xml(resp.text)
        except ET.ParseError as e:
            logger.error("XML identity parse failed: %s", e)
            return []

    def get_identity(self, identity_id: str) -> dict[str, Any] | None:
        resp = self._request(
            "GET", f"/identities/{identity_id}.json",
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            return None
        try:
            body = resp.json()
            raw = body.get("data", body) if isinstance(body, dict) else body
            if isinstance(raw, dict):
                return self._normalize_identity(raw)
        except Exception as e:  # noqa: BLE001
            logger.error("get_identity %s parse failed: %s", identity_id, e)
        return None

    def get_identity_tokens(self, identity_id: str) -> list[dict[str, Any]]:
        resp = self._request("GET", f"/identities/{identity_id}/tokens.xml")
        if resp.status_code != 200:
            return []
        try:
            return self._parse_tokens_xml(resp.text, identity_id)
        except ET.ParseError as e:
            logger.error("XML token parse failed for %s: %s", identity_id, e)
            return []

    def update_token_status(
        self, identity_id: str, token_id: str,
        avigilon_status: str, current_token_data: dict[str, Any] | None = None,
    ) -> bool:
        p = self._prefix
        td = current_token_data or {}
        form = {
            "utf8": "✓",
            "_method": "put",
            "authenticity_token": self.csrf_token,
            f"token[{p}Tokenstatus]": avigilon_status,
            f"token[{p}Internalnumber]": td.get("internal_number", ""),
            f"token[{p}Embossednumber]": td.get("embossed_number", ""),
            f"token[{p}PIN]": td.get("pin", ""),
            f"token[{p}TokenType]": td.get("token_type", "0"),
            f"token[{p}Tokenlevel]": td.get("level", "0"),
            f"token[{p}TokenMobileAppType]": "0",
            f"token[{p}TokenOrigoMobileIdType]": "0",
            f"token[{p}Download]": "TRUE",
            f"token[{p}TokenUnitofUpdatePeriod]": "0",
            f"token[{p}Tokennoexpire]": "FALSE",
            f"{p}Issuedate": td.get("issue_date", ""),
            f"{p}Activatedate": td.get("activate_date", ""),
            f"{p}Deactivatedate": td.get("deactivate_date", ""),
            "enrollVirdiAfter": "false",
        }
        resp = self._request(
            "POST", f"/identities/{identity_id}/tokens/{token_id}",
            data=form,
            headers={"Referer": f"{self.base_url}/identities/{identity_id}/tokens/{token_id}/edit"},
        )
        return resp.status_code == 302

    # ------------------------------------------------------------------
    # XML parsing helpers
    # ------------------------------------------------------------------

    def _find_prefixed(self, elem: ET.Element, suffix: str) -> ET.Element | None:
        for prefix in self._XML_PREFIXES:
            found = elem.find(f"{prefix}{suffix}")
            if found is not None:
                if self._prefix != prefix:
                    logger.info("Detected Avigilon field prefix from XML: %r", prefix)
                    self._prefix = prefix
                return found
        return None

    def _prefixed_text(self, elem: ET.Element, suffix: str) -> str:
        found = self._find_prefixed(elem, suffix)
        return found.text if found is not None and found.text else ""

    @classmethod
    def _prefixed_get(cls, d: dict[str, Any], suffix: str, default: str = "") -> Any:
        for prefix in cls._XML_PREFIXES:
            val = d.get(f"{prefix}{suffix}")
            if val not in (None, ""):
                return val
        return default

    def _parse_identities_xml(self, xml_text: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        results = []
        for identity_elem in root.findall("identity"):
            cn_elem = identity_elem.find(".//cn")
            cn = cn_elem.text if cn_elem is not None else ""
            first_name = self._prefixed_text(identity_elem, "Fname")
            last_name = self._prefixed_text(identity_elem, "Lname")
            avigilon_name = self._prefixed_text(identity_elem, "Name")
            if not first_name and not last_name and avigilon_name:
                parts = [p.strip() for p in avigilon_name.split(",")]
                last_name = parts[0] if parts else ""
                first_name = parts[1] if len(parts) > 1 else ""
            full_name = f"{first_name} {last_name}".strip() or avigilon_name
            raw_status = self._prefixed_text(identity_elem, "Idstatus") or "1"
            results.append({
                "id": cn,
                "first_name": first_name or "",
                "last_name": last_name or "",
                "full_name": full_name,
                "email": self._prefixed_text(identity_elem, "identityEmailaddress"),
                "phone": self._prefixed_text(identity_elem, "identityPhone"),
                "work_phone": self._prefixed_text(identity_elem, "identityWorkphone"),
                "status": self._normalize_identity_status(raw_status),
                "title": self._prefixed_text(identity_elem, "identityTitle"),
                "department": self._prefixed_text(identity_elem, "identityDepartment"),
            })
        return results

    def _parse_tokens_xml(self, xml_text: str, identity_id: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        results = []
        for token_elem in root.findall("token"):
            cn_elem = token_elem.find(".//cn")
            cn = cn_elem.text if cn_elem is not None else ""

            def _text(suffix: str, _elem: ET.Element = token_elem) -> str:
                return self._prefixed_text(_elem, suffix)

            results.append({
                "id": cn,
                "identity_id": identity_id,
                "internal_number": _text("Internalnumber"),
                "embossed_number": _text("Embossednumber"),
                "pin": _text("PIN"),
                "status": _text("Tokenstatus") or "1",
                "token_type": _text("TokenType") or "0",
                "level": _text("Tokenlevel") or "0",
                "issue_date": _text("Issuedate"),
                "activate_date": _text("Activatedate"),
                "deactivate_date": _text("Deactivatedate"),
            })
        return results

    def _normalize_identity(self, raw: dict[str, Any]) -> dict[str, Any]:
        if "attributes" in raw:
            attrs = raw.get("attributes", {})
            identity_id = raw.get("id", "") or attrs.get("cn", "")
        else:
            attrs = raw
            identity_id = raw.get("cn", "") or raw.get("id", "")

        first_name = str(self._prefixed_get(attrs, "Fname") or "")
        last_name = str(self._prefixed_get(attrs, "Lname") or "")
        avigilon_name = str(self._prefixed_get(attrs, "Name") or "")
        if not first_name and not last_name and avigilon_name:
            parts = [p.strip() for p in avigilon_name.split(",")]
            last_name = parts[0] if parts else ""
            first_name = parts[1] if len(parts) > 1 else ""
        full_name = f"{first_name} {last_name}".strip() or avigilon_name
        raw_status = str(self._prefixed_get(attrs, "Idstatus") or attrs.get("status", "") or "")

        return {
            "id": identity_id,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "email": str(self._prefixed_get(attrs, "identityEmailaddress") or ""),
            "phone": str(self._prefixed_get(attrs, "identityPhone") or ""),
            "work_phone": str(self._prefixed_get(attrs, "identityWorkphone") or ""),
            "status": self._normalize_identity_status(raw_status),
            "title": str(self._prefixed_get(attrs, "identityTitle") or ""),
            "department": str(self._prefixed_get(attrs, "identityDepartment") or ""),
        }

    @staticmethod
    def _normalize_identity_status(raw: str) -> str:
        mapping = {"active": "1", "inactive": "2", "not yet active": "3", "expired": "4"}
        lower = (raw or "").lower()
        if lower in mapping:
            return mapping[lower]
        if raw in ("1", "2", "3", "4"):
            return raw
        return "1"

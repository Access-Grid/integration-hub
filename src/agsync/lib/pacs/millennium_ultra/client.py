"""HTTP client for Millennium Ultra (MGI Access) hosted web interface.

There is no API — this drives the same MVC endpoints the browser does, using
a session cookie captured from a real login.

Authentication is deliberately *not* performed here. The login page is behind
a reCAPTCHA v2 checkbox that is verified server-side (a POST with an empty
`g-recaptcha-response` is rejected with "Invalid Captcha !" before credentials
are even evaluated), so a human signs in and we adopt the resulting
`.AspNet.UltraAuth` cookie. This client therefore never sees a password and
stores none — see `.claude/MILLENNIUM_ULTRA_PLAN.md` §6.

The two endpoints that matter:

  GET /Cardholders/CardholdersHelper/GetList/{id}?firstLetter={65..90|0}
      Cheap JSON roster, bucketed by first letter of the surname. 27 calls
      cover everyone (~350 KB on the reference tenant).

  GET /Cardholders/Cardholders/Index/{id}?pw=200
      The cardholder's detail page — the *only* place card data lives.
      ~90 KB and ~0.4 s each, so fetching them is the expensive part of a
      cycle and is done concurrently.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import httpx

from .parse import ROSTER_BUCKETS

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0
# The embedded app is picky about non-browser clients; mirror a real UA.
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

AUTH_COOKIE = ".AspNet.UltraAuth"
COMPANY_COOKIE = "UltraCompanyName"

# Detail-page fetches are the bottleneck. Six keeps a cold full scan of the
# reference tenant's 1,842 cardholders near two minutes without hammering it.
DEFAULT_MAX_WORKERS = 6


class MillenniumUltraError(Exception):
    """Any non-recoverable problem talking to Millennium Ultra."""


class SessionExpired(MillenniumUltraError):
    """The captured session is no longer valid — a human must reconnect.

    Kept distinct from transport errors on purpose: this one cannot heal
    itself, so the engine pauses and the UI asks for a new sign-in, whereas a
    network blip is worth retrying.
    """


def parse_pasted_session(text: str) -> dict[str, str]:
    """Pull session cookies out of whatever the operator pasted.

    Accepts, in order of how people actually produce it:
      * a full `Copy as cURL` command (cookies live in -b/--cookie or a
        `-H 'cookie: ...'` header)
      * a raw Cookie header, `UltraCompanyName=ICON; .AspNet.UltraAuth=...`
      * a bare `.AspNet.UltraAuth` value

    Returns only the cookies we care about; everything else (analytics, etc.)
    is dropped.
    """
    text = (text or "").strip()
    if not text:
        return {}

    blobs: list[str] = []
    for pattern in (
        r"(?:-b|--cookie)\s+'([^']*)'",
        r'(?:-b|--cookie)\s+"([^"]*)"',
        r"""-H\s+['"]\s*cookie:\s*([^'"]*)['"]""",
    ):
        blobs.extend(re.findall(pattern, text, flags=re.IGNORECASE))
    if not blobs:
        blobs = [text]

    found: dict[str, str] = {}
    for blob in blobs:
        for part in blob.split(";"):
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            name, value = name.strip(), value.strip()
            if name in (AUTH_COOKIE, COMPANY_COOKIE) and value:
                found[name] = value

    if not found and "=" not in text and len(text) > 40 and " " not in text:
        # A bare token pasted on its own.
        found[AUTH_COOKIE] = text
    return found


def base_url_from_paste(text: str) -> str:
    """Best-effort origin from a pasted cURL command, for prefilling the URL."""
    m = re.search(r"""https?://[^\s'"]+""", text or "")
    if not m:
        return ""
    parsed = urlparse(m.group(0))
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""


class MillenniumUltraClient:
    def __init__(
        self,
        base_url: str,
        session_cookies: dict[str, str] | None = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        verify: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._max_workers = max(1, int(max_workers or DEFAULT_MAX_WORKERS))
        cookies = dict(session_cookies or {})
        # The app writes this from JavaScript on every page; some handlers
        # read it when rendering dates. Harmless to pin to UTC.
        cookies.setdefault("timeoffset", "0")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=HTTP_TIMEOUT,
            follow_redirects=False,  # a 302 to /Account/Login is our expiry signal
            verify=verify,
            cookies=cookies,
            headers={"User-Agent": HTTP_USER_AGENT},
            # Injected by tests; production always uses httpx's default.
            transport=transport,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MillenniumUltraClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @property
    def company(self) -> str:
        return self._client.cookies.get(COMPANY_COOKIE) or ""

    # -- plumbing ----------------------------------------------------------

    def _get(self, path: str, *, xhr: bool = False, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
        try:
            response = self._client.get(path, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise MillenniumUltraError(f"{type(e).__name__}: {e}") from e

        # Forms auth bounces unauthenticated requests back to the login page.
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if "/Account/Login" in location:
                raise SessionExpired(
                    "Millennium Ultra session has expired — sign in again to reconnect."
                )
            raise MillenniumUltraError(f"Unexpected redirect to {location!r} for {path}")

        if response.status_code >= 400:
            raise MillenniumUltraError(f"HTTP {response.status_code} for {path}")
        return response

    # -- reads -------------------------------------------------------------

    def list_roster(self) -> list[dict[str, Any]]:
        """Every cardholder, from the 27 letter buckets. Cheap: no card data."""
        seen: dict[str, dict[str, Any]] = {}
        for bucket in ROSTER_BUCKETS:
            rows = self._get(
                f"/Cardholders/CardholdersHelper/GetList/0?firstLetter={bucket}",
                xhr=True,
                headers={"Referer": f"{self.base_url}/Cardholders/Cardholders"},
            ).json()
            for row in rows or []:
                pid = str(row.get("ID"))
                if pid and pid != "None":
                    seen[pid] = row
        logger.info("Millennium Ultra: roster has %d cardholders", len(seen))
        return list(seen.values())

    def get_cardholder_html(self, cardholder_id: str | int) -> str:
        return self._get(
            f"/Cardholders/Cardholders/Index/{cardholder_id}?pw=200",
            headers={"Referer": f"{self.base_url}/Cardholders/Cardholders"},
        ).text

    def get_cardholders_html(self, ids: list[str]) -> dict[str, str]:
        """Fetch many detail pages concurrently.

        A failure on one cardholder returns no entry for it rather than
        aborting the sweep — one unreadable record should not cost us the
        whole cycle. SessionExpired is the exception: it will affect every
        subsequent request, so it propagates.
        """
        out: dict[str, str] = {}
        if not ids:
            return out

        expired: list[SessionExpired] = []

        def fetch(pid: str) -> tuple[str, str | None]:
            try:
                return pid, self.get_cardholder_html(pid)
            except SessionExpired as e:
                expired.append(e)
                return pid, None
            except MillenniumUltraError as e:
                logger.warning("Millennium Ultra: cardholder %s unreadable: %s", pid, e)
                return pid, None

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            for pid, html in pool.map(fetch, ids):
                if html is not None:
                    out[pid] = html

        if expired and not out:
            raise expired[0]
        return out

    def test_connection(self) -> tuple[bool, str]:
        """Confirm the session works *and* that it can see the roster.

        Reports the cardholder count rather than a bare OK: a valid cookie and
        an account with visibility of the cardholder list are different
        failures, and only the count distinguishes them.
        """
        try:
            roster = self.list_roster()
        except SessionExpired as e:
            return False, str(e)
        except MillenniumUltraError as e:
            return False, f"Millennium Ultra: {e}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

        who = self.company or "Millennium Ultra"
        if not roster:
            return False, f"Connected as {who}, but no cardholders are visible to this account."
        return True, f"Connected as {who} — {len(roster):,} cardholders found."

"""HTTP client for a Millennium Ultra web install.

Millennium Ultra has no API — this drives the same ASP.NET MVC screens an
operator uses, carrying the forms-auth cookie that the AG Connect side-car
captured (the login is captcha-gated, so we can never authenticate
headlessly; see agsync.connect).

Endpoints, all verified against a live install (hosted8.mgiaccess.com):

  GET  /Cardholders/CardholdersHelper/GetList/{ignored}?firstLetter=<ascii>
        JSON roster for one initial letter. The path segment is the row the
        UI has selected and does not affect the result, so we send 0.
  GET  /Cardholders/Cardholders/Index/{id}?pw=200
        The cardholder detail screen. Also the only place card formats are
        enumerated (in each slot's <select>), and the source of the
        __RequestVerificationToken that DeleteCard requires.
  POST /Cardholders/Cardholders/Index/{id}
        Full-form save. See html_form — every field must be echoed back.
  GET  /Cardholders/Cardholders/ValidateEncodedCardNumber?...
        "true" when a facility-code/card-number pair is free.
  POST /Cardholders/Cardholders/DeleteCard   (ID, CardID)
        Removes a card from its slot. Used when AccessGrid has deleted a
        credential we wrote and the slot should be released; suspension is
        a different thing and unchecks Card_N_Active instead.

There is no endpoint for creating a card: the full-form save does it. An
empty Card_N_CardID tells Millennium to mint one and assign the id itself,
a populated one updates that card in place. Which is the other reason every
field has to be echoed back — the save replaces the whole record, card
slots included, so a field left out is a field erased.

Session expiry is the failure mode that matters, and it does not announce
itself. A dead cookie has been observed producing three different shapes,
none of which looks like an error:

  * the HTML screens 302 to /Account/LogIn
  * the JSON endpoints return an empty 200 body
  * the JSON endpoints return a valid, empty ``[]`` while the HTML screens
    answer 500

The third is the awkward one: a well-formed empty roster is
indistinguishable from a PACS with no cardholders. It used to be the
dangerous one too, reading downstream as "every cardholder was deleted",
but phase 3 now abandons a cycle whose snapshot has no people at all and
declines to act on any cardholder whose page failed to load. So a dead
cookie no longer deletes anything.

What it does instead is stop, quietly, looking exactly like a healthy
install with nothing to do — which is why all three shapes are still raised
as MillenniumAuthError, and why the engine asks for a reconnect rather than
inferring one. An install that genuinely has no cardholders is misreported
by that rule; the trade is deliberate, because being wrong in this
direction prompts a human and being wrong in the other direction is
invisible.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ..base import PacsAuthExpired
from . import export as export_mod
from .html_form import CardholderForm

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 60.0

# How long to wait for the cardholder export job, and how often to ask. It
# has completed in under a second on a healthy install and 502'd outright on
# a struggling one, so the ceiling is about not hanging a cycle rather than
# about the job being slow.
EXPORT_TIMEOUT_S = 90.0
EXPORT_POLL_INTERVAL_S = 1.0

# What to ask the export for. Images are the field that matters by its
# absence: with them the job 504s outright on this install, which is what
# made the endpoint look unusable at first. The rest is the smallest set
# that answers "who carries a trigger card, and what is in their slots" —
# Employee ID is included because it is cheap and occasionally the only
# thing distinguishing two people with the same name.
_EXPORT_FIELDS = (
    (1, "First Name"),
    (2, "Last Name"),
    (4, "Employee ID"),
    # Contact details, which reach us nowhere else in time. The detail page
    # carries them too, but it is read after list_people has already built
    # the Person — so on the cycle a cardholder is first seen, which is the
    # cycle they are provisioned on, both were empty and the pass went out
    # to a synthesized address with no phone number.
    (11, "Phone"),
    (13, "E-Mail"),
    (14, "Current Status"),
    (16, "Encoded Card No."),
    (17, "Activation Date"),
    (18, "Expiration Date"),
    (19, "Active"),
    (20, "Card Format"),
    (21, "Facility Code"),
)

# Smallest gap between two requests to the install, across every client in
# this process. Millennium is an ASP.NET UI sized for one operator clicking
# around, and an unpaced sweep put ~5.4 requests a second through it for
# minutes at a time — which is where the read timeouts came from. 100ms
# costs about 40 seconds on a 400-page sweep.
#
# Process-wide rather than per-client on purpose: the sync engine is not the
# only caller. A /settings or /connect page build its own adapter on a
# request thread, and those were timing out *during* a sweep, so pacing that
# only covered the engine would miss exactly the collision that showed up.
MIN_REQUEST_INTERVAL_S = 0.1

# Nothing here is concurrent by design — the engine is one thread — but a web
# request can overlap a cycle, so the ceiling is stated rather than assumed.
MAX_CONNECTIONS = 4
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

AUTH_COOKIE = "Ultra"
COMPANY_COOKIE = "UltraCoreCompanyName"
TIMEOFFSET_COOKIE = "timeoffset"
# The anti-forgery cookie, which only the export endpoints check. ASP.NET
# Core suffixes its name per application, so unlike the three above there
# is no constant for it: the name is captured at sign-in and travels with
# the session. This prefix is only for recognising it in a cookie jar.
ANTIFORGERY_COOKIE_PREFIX = ".AspNetCore.Antiforgery."

# Millennium gives every cardholder exactly this many card slots; the detail
# page declares it as `cardsPerCardholder = 3`.
CARD_SLOTS = (1, 2, 3)

_EXPORT_REQUEST = {
    "format": 1,
    # Millennium's own spelling; correcting it silently disables the flag.
    "IncudeImages": False,
    "tenants": [0],
    "cards": list(CARD_SLOTS),
    "fields": [{"ID": fid, "Name": name} for fid, name in _EXPORT_FIELDS],
}

# The roster endpoint is keyed by the ASCII code of the surname's initial.
# Only A-Z is swept: names starting with a digit, punctuation or an accented
# character have no bucket of their own on this install.
LETTER_CODES = tuple(range(ord("A"), ord("Z") + 1))


class MillenniumError(RuntimeError):
    """Any non-auth failure talking to Millennium."""


class MillenniumAuthError(MillenniumError, PacsAuthExpired):
    """The captured session is gone — a human must re-run AG Connect."""


def normalize_base_url(url: str) -> str:
    """Reduce whatever the operator pasted to the site's origin.

    The setup field asks for the Millennium URL, and operators reasonably
    paste the address bar from their login page. Every request path here is
    absolute, so anything after the host has to go.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""


# Guards the pace clock below. Held only for the length of the wait, which
# is what makes callers queue behind one another rather than all sleeping
# the same 100ms and then firing together.
_PACE_LOCK = threading.Lock()
_last_request_at = 0.0


class MillenniumUltraClient:
    def __init__(
        self,
        base_url: str,
        auth_cookie: str,
        company_name: str = "",
        time_offset: str = "",
        request_token: str = "",
        request_token_cookie: str = "",
    ):
        self.base_url = normalize_base_url(base_url)
        if not self.base_url:
            raise MillenniumError("Millennium base URL is not configured")
        cookies = {AUTH_COOKIE: auth_cookie}
        if request_token and request_token_cookie:
            # Both halves or neither: the name is per-application, so a
            # value sent under a guessed name is no better than nothing.
            # The export endpoints are the only ones that check it. Missing,
            # they answer 502 rather than 403, which reads as the server
            # being unwell rather than as a rejected request.
            cookies[request_token_cookie] = request_token
        if company_name:
            cookies[COMPANY_COOKIE] = company_name
        if time_offset:
            # The server renders and parses the MM/DD/YYYY form dates against
            # this offset, so pinning it keeps activation/expiration times
            # from drifting by the operator's UTC offset.
            cookies[TIMEOFFSET_COOKIE] = time_offset
        self._http = httpx.Client(
            cookies=cookies,
            headers={"User-Agent": HTTP_USER_AGENT},
            timeout=HTTP_TIMEOUT,
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=MAX_CONNECTIONS,
            ),
        )

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _pace() -> None:
        """Block until MIN_REQUEST_INTERVAL_S has passed since the last call."""
        global _last_request_at
        with _PACE_LOCK:
            wait = _last_request_at + MIN_REQUEST_INTERVAL_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_request_at = time.monotonic()

    # -- plumbing --------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _check_auth(response: httpx.Response) -> None:
        location = response.headers.get("location", "")
        if response.is_redirect and "/Account/Log" in location:
            raise MillenniumAuthError(
                "Millennium session expired — reconnect via AG Connect"
            )

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        ajax: bool = False,
        server_error_means_expired: bool = False,
    ) -> httpx.Response:
        headers = {"Accept": "text/html"}
        if ajax:
            headers = {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            }
        self._pace()
        response = self._http.get(self._url(path), params=params, headers=headers)
        self._check_auth(response)
        if response.status_code >= 500 and server_error_means_expired:
            # A dead session makes the cardholder screens throw rather than
            # redirect. A genuine server fault looks the same from here, so
            # this can cry wolf — but the cost is a reconnect prompt, against
            # a sync that otherwise stops without saying anything.
            raise MillenniumAuthError(
                f"Millennium returned HTTP {response.status_code} for a cardholder "
                "screen — the session has probably expired"
            )
        if response.status_code >= 400:
            raise MillenniumError(f"GET {path} -> HTTP {response.status_code}")
        return response

    # -- reads -----------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        """Prove the session can actually read, not merely that it exists."""
        try:
            cardholder_id = self.first_cardholder_id()
        except MillenniumAuthError as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        return True, f"Connected — cardholders are readable (e.g. {cardholder_id})"

    def _list_letter(self, letter_code: int) -> list[dict[str, Any]]:
        response = self._get(
            "/Cardholders/CardholdersHelper/GetList/0",
            params={"firstLetter": str(letter_code), "_": str(int(time.time() * 1000))},
            ajax=True,
        )
        body = response.text.strip()
        if not body:
            # An authenticated miss returns "[]"; a dead cookie returns an
            # empty body with a 200, which would otherwise read as "no
            # cardholders" and trip phase 3's deletion logic.
            raise MillenniumAuthError(
                "Millennium returned an empty roster — the session has expired"
            )
        try:
            rows = json.loads(body)
        except json.JSONDecodeError as e:
            raise MillenniumError(f"Roster for letter {letter_code} was not JSON") from e
        return rows if isinstance(rows, list) else []

    def first_cardholder_id(self) -> str:
        """Any cardholder's id, for reads that just need a detail page.

        Card formats are only readable off a card slot's <select>, and that
        lookup should not pay for a full A-Z roster sweep, so stop at the
        first letter that returns anybody. Finding nobody at all means the
        same thing here as it does for a full sweep.
        """
        for code in LETTER_CODES:
            rows = self._list_letter(code)
            if rows:
                return str(rows[0].get("ID"))
        raise MillenniumAuthError(
            "Millennium returned no cardholders under any letter — the "
            "session has probably expired"
        )

    def list_cardholders(self) -> list[dict[str, Any]]:
        """Every cardholder, swept A–Z and de-duplicated by ID.

        An entirely empty sweep is treated as an expired session rather than
        as a PACS with no cardholders. Every letter answering "[]" is what a
        dead cookie looks like here, and the alternative reading stops the
        sync silently — phase 3 declines to delete anything, so nothing moves
        and nothing is reported.
        """
        seen: dict[int, dict[str, Any]] = {}
        for code in LETTER_CODES:
            for row in self._list_letter(code):
                cid = row.get("ID")
                if cid is not None:
                    seen[cid] = row
        if not seen:
            raise MillenniumAuthError(
                "Millennium returned no cardholders under any letter — the "
                "session has probably expired"
            )
        logger.info("Millennium: %d cardholder(s) across A-Z", len(seen))
        return list(seen.values())

    def get_cardholder_form(self, cardholder_id: str | int) -> CardholderForm:
        """Fetch a cardholder's detail screen as a replayable form.

        This doubles as the read-modify-write baseline: the parsed form is
        both the current state and the body of the next save.
        """
        response = self._get(
            f"/Cardholders/Cardholders/Index/{cardholder_id}",
            params={"pw": "200"},
            server_error_means_expired=True,
        )
        return CardholderForm.parse(response.text)

    def card_formats(self, cardholder_id: str | int) -> list[tuple[str, str]]:
        """(id, label) for every card format this install defines.

        There is no admin endpoint that lists formats; the only place they
        appear is the <select> on a card slot, so we read them off any
        cardholder's detail page.
        """
        form = self.get_cardholder_form(cardholder_id)
        for slot in CARD_SLOTS:
            options = form.options(f"Card_{slot}_CardFormat")
            if options:
                return [(o.value, o.label.strip()) for o in options if o.value]
        return []

    def card_number_is_free(
        self, slot: int, card_number: str, facility_code: str, card_format: str
    ) -> bool:
        """Ask Millennium whether a facility-code/card-number pair is unused.

        Returns True only on an explicit "true" — a malformed answer is
        treated as "taken" so we never write a colliding card.
        """
        response = self._get(
            "/Cardholders/Cardholders/ValidateEncodedCardNumber",
            params={
                f"Card_{slot}_EncodedCardNumber": card_number,
                f"Card_{slot}_CardID": "",
                f"Card_{slot}_FaciltyCode": facility_code,
                f"Card_{slot}_CardFormat": card_format,
                "_": str(int(time.time() * 1000)),
            },
            ajax=True,
        )
        return response.text.strip().lower() == "true"

    # -- bulk read -------------------------------------------------------

    def export_cardholders(self) -> str:
        """Every cardholder's slots in three requests, as CSV.

        A queued job, not an endpoint: start it, poll until the status says
        it finished, then fetch the file that status names. See export.py
        for what the CSV holds and what it leaves out.

        Raises MillenniumError if the job fails or does not finish in time.
        The caller is expected to fall back to reading detail pages — this
        is an optimisation, and a PACS that will not export is not a PACS
        that cannot be synced.
        """
        started = self._post_json(
            "/DatabaseFunctions/ExportCardholders/ExportCardholdersNow",
            _EXPORT_REQUEST,
        )
        if started is not True:
            raise MillenniumError(f"Millennium refused to start the export: {started!r}")

        deadline = time.time() + EXPORT_TIMEOUT_S
        while time.time() < deadline:
            status = self._export_status()
            if not isinstance(status, dict):
                raise MillenniumError(f"Unreadable export status: {status!r}")
            if status.get("Failed") or (status.get("Error") or ""):
                raise MillenniumError(
                    f"Millennium's export failed: {status.get('Error') or 'no detail'}"
                )
            if status.get("Completed") and status.get("Success"):
                # Context still names the archive, and an empty one means
                # the job finished without producing anything — worth
                # refusing rather than downloading whatever was left over
                # from a previous run. The name no longer forms the URL.
                name = status.get("Context") or ""
                if not name:
                    raise MillenniumError("Export finished without naming its file")
                logger.debug("Millennium's export produced %s", name)
                return export_mod.unpack(self._fetch_export_file())
            time.sleep(EXPORT_POLL_INTERVAL_S)

        raise MillenniumError(
            f"Millennium's export did not finish within {EXPORT_TIMEOUT_S:.0f}s"
        )

    def _export_status(self) -> Any:
        """How far along the queued export is.

        A GET under /api on the ASP.NET Core build, where it used to be a
        POST to /Home/GetLongOperationStatus. The trailing `_` is the UI's
        own cache-buster, kept because the response is the one thing here
        that must never be served from a cache: a stale "Completed" would
        send us to download the previous run's file.
        """
        response = self._get(
            "/api/LongOperationStatus",
            params={
                "operationtype": export_mod.OPERATION,
                "_": str(int(time.time() * 1000)),
            },
            ajax=True,
        )
        try:
            return response.json()
        except ValueError as e:
            raise MillenniumError(f"Unreadable export status: {e}") from e

    def _fetch_export_file(self) -> bytes:
        """The zip the finished job left behind.

        Addressed by what kind of export it was, not by the filename the
        status gave: the Core build serves the caller's most recent export
        from /WebServices/FileDownload, and has no route that takes a name.
        """
        response = self._get(
            "/WebServices/FileDownload/",
            params={"FileType": export_mod.OPERATION},
        )
        return response.content

    def _post_json(self, path: str, payload: dict[str, Any]) -> Any:
        """POST JSON the way the UI's own XHRs do, and read JSON back."""
        self._pace()
        response = self._http.post(
            self._url(path),
            json=payload,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.base_url,
                "Referer": self._url("/DatabaseFunctions/ExportCardholders"),
            },
        )
        self._check_auth(response)
        if response.status_code >= 400:
            raise MillenniumError(f"POST {path} -> HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as e:
            raise MillenniumError(f"POST {path} did not answer JSON") from e

    # -- writes ----------------------------------------------------------

    def save_cardholder(self, cardholder_id: str | int, form: CardholderForm) -> bool:
        """Re-post a full cardholder form. True when Millennium accepted it.

        A successful save answers 302 back to the cardholder screen. A 200
        means the server re-rendered the page with validation errors, which
        is a rejection — the record is unchanged.
        """
        content_type, body = form.to_multipart()
        referer = self._url(f"/Cardholders/Cardholders/Index/{cardholder_id}?pw=200")
        self._pace()
        response = self._http.post(
            self._url(f"/Cardholders/Cardholders/Index/{cardholder_id}"),
            content=body,
            headers={
                "Content-Type": content_type,
                "Accept": "text/html",
                "Origin": self.base_url,
                "Referer": referer,
            },
        )
        self._check_auth(response)
        if response.is_redirect:
            return True
        if response.status_code == 200:
            logger.error(
                "Millennium rejected the save for cardholder %s (validation error)",
                cardholder_id,
            )
            return False
        raise MillenniumError(
            f"Save for cardholder {cardholder_id} -> HTTP {response.status_code}"
        )

    def delete_card(self, cardholder_id: str | int, card_id: str, token: str) -> bool:
        """Remove a card from its slot.

        Used when AccessGrid has deleted a credential we wrote, so the slot
        it occupies can be released — a cardholder has only three, and one
        held by a dead credential is one a real device cannot have.
        Suspending is a different thing: it unchecks Card_N_Active and
        leaves the card where it is.
        """
        self._pace()
        response = self._http.post(
            self._url("/Cardholders/Cardholders/DeleteCard"),
            data={
                "__RequestVerificationToken": token,
                "ID": str(cardholder_id),
                "CardID": str(card_id),
            },
            headers={
                "Origin": self.base_url,
                "Referer": self._url(
                    f"/Cardholders/Cardholders/Index/{cardholder_id}?pw=200"
                ),
            },
        )
        self._check_auth(response)
        return response.status_code == 200 and response.text.strip().lower() == "true"

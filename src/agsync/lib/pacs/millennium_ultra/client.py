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
        The cardholder detail screen. This is also the only place card
        formats are enumerated (in each slot's <select>).
  POST /Cardholders/Cardholders/Index/{id}
        Full-form save. See html_form — every field must be echoed back.
  GET  /Cardholders/Cardholders/ValidateEncodedCardNumber?...
        "true" when a facility-code/card-number pair is free.
  POST /Cardholders/Cardholders/DeleteCard   (ID, CardID)
        Removes a card from a slot. Used only to roll back a half-written
        provision; normal suspension unchecks Card_N_Active instead.

Session expiry is the failure mode that matters. A dead cookie makes the
HTML screens 302 to /Account/LogIn and makes the JSON endpoints return an
empty 200 — neither looks like an error, so both are detected explicitly
and raised as MillenniumAuthError. The engine turns that into a
"reconnect" notification rather than mistaking an expired session for a
cardholder roster that suddenly went empty.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ..base import PacsAuthExpired
from .html_form import CardholderForm

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 60.0
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

AUTH_COOKIE = ".AspNet.UltraAuth"
COMPANY_COOKIE = "UltraCompanyName"
TIMEOFFSET_COOKIE = "timeoffset"

# Millennium gives every cardholder exactly this many card slots; the detail
# page declares it as `cardsPerCardholder = 3`.
CARD_SLOTS = (1, 2, 3)

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


class MillenniumUltraClient:
    def __init__(
        self,
        base_url: str,
        auth_cookie: str,
        company_name: str = "",
        time_offset: str = "",
    ):
        self.base_url = normalize_base_url(base_url)
        if not self.base_url:
            raise MillenniumError("Millennium base URL is not configured")
        cookies = {AUTH_COOKIE: auth_cookie}
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
        )

    def close(self) -> None:
        self._http.close()

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

    def _get(self, path: str, params: dict[str, Any] | None = None, ajax: bool = False) -> httpx.Response:
        headers = {"Accept": "text/html"}
        if ajax:
            headers = {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            }
        response = self._http.get(self._url(path), params=params, headers=headers)
        self._check_auth(response)
        if response.status_code >= 400:
            raise MillenniumError(f"GET {path} -> HTTP {response.status_code}")
        return response

    # -- reads -----------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        try:
            rows = self._list_letter(LETTER_CODES[0])
        except MillenniumAuthError as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        return True, f"Connected — {len(rows)} cardholder(s) under 'A'"

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
        first letter that returns anybody.
        """
        for code in LETTER_CODES:
            rows = self._list_letter(code)
            if rows:
                return str(rows[0].get("ID"))
        return ""

    def list_cardholders(self) -> list[dict[str, Any]]:
        """Every cardholder, swept A–Z and de-duplicated by ID."""
        seen: dict[int, dict[str, Any]] = {}
        for code in LETTER_CODES:
            for row in self._list_letter(code):
                cid = row.get("ID")
                if cid is not None:
                    seen[cid] = row
        logger.info("Millennium: %d cardholder(s) across A-Z", len(seen))
        return list(seen.values())

    def get_cardholder_form(self, cardholder_id: str | int) -> CardholderForm:
        """Fetch a cardholder's detail screen as a replayable form.

        This doubles as the read-modify-write baseline: the parsed form is
        both the current state and the body of the next save.
        """
        response = self._get(
            f"/Cardholders/Cardholders/Index/{cardholder_id}", params={"pw": "200"}
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

    # -- writes ----------------------------------------------------------

    def save_cardholder(self, cardholder_id: str | int, form: CardholderForm) -> bool:
        """Re-post a full cardholder form. True when Millennium accepted it.

        A successful save answers 302 back to the cardholder screen. A 200
        means the server re-rendered the page with validation errors, which
        is a rejection — the record is unchanged.
        """
        content_type, body = form.to_multipart()
        referer = self._url(f"/Cardholders/Cardholders/Index/{cardholder_id}?pw=200")
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

        Only used to roll back a provision we could not finish; suspending a
        credential unchecks Card_N_Active and leaves the card in place.
        """
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

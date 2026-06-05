"""HTTP client for Avigilon Alta Access (cloud).

Avigilon Alta is the rebranded OpenPath product; its API is served by
`helium.prod.openpath.com` (the "Helium" backend). Auth is a two-step
bearer-token flow:

  1. POST /auth/determineLoginCandidateNamespaces {email}
       → pick the namespace (org) the email belongs to.
  2. POST /auth/login {email, password, namespaceId}
       → response body carries `data.token`, a ~14-day JWT. That raw
         token (no "Bearer " prefix) is sent as the `authorization`
         header on every subsequent request.

The host is fixed — Alta is always cloud-hosted — so unlike the on-prem
Avigilon Unity client this one takes no host parameter.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

HELIUM_HOST = "helium.prod.openpath.com"
HTTP_TIMEOUT = 30.0
HTTP_USER_AGENT = "AGSyncTool/0.1"

# Re-login this many seconds before the token's stated expiry so a long
# sync cycle can't have the token die mid-flight.
TOKEN_EXPIRY_SKEW = timedelta(minutes=5)

# How many users/credentials to pull per page.
PAGE_SIZE = 100


class AltaAuthError(Exception):
    pass


class AltaAPIError(Exception):
    pass


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class AltaClient:
    """Session-based bearer-token client for Avigilon Alta Access."""

    def __init__(self, email: str, password: str):
        self.base_url = f"https://{HELIUM_HOST}"
        self.email = email
        self.password = password

        self._client = httpx.Client(
            base_url=self.base_url,
            verify=True,
            timeout=HTTP_TIMEOUT,
            headers={
                "User-Agent": HTTP_USER_AGENT,
                "Accept": "application/json",
                "Origin": "https://access.alta.avigilon.com",
                "Referer": "https://access.alta.avigilon.com/",
            },
            follow_redirects=False,
        )

        self._token: str = ""
        self._token_expires_at: datetime | None = None
        self._namespace_id: int | None = None
        self._org_id: int | None = None
        self._user_id: int | None = None
        self._identity_id: int | None = None

        logger.info("AltaClient init: base_url=%s email=%r", self.base_url, self.email)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AltaClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def org_id(self) -> int | None:
        return self._org_id

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _determine_namespace(self) -> None:
        """Resolve which namespace (org) the login email belongs to."""
        resp = self._client.post(
            "/auth/determineLoginCandidateNamespaces",
            json={"email": self.email},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise AltaAuthError(
                f"determineLoginCandidateNamespaces returned HTTP {resp.status_code}"
            )
        data = (resp.json() or {}).get("data") or []
        if not data:
            raise AltaAuthError(f"No login namespace found for {self.email!r}")
        if len(data) > 1:
            # Multiple orgs share this email. We pick the first and log it;
            # a future revision could surface a chooser in the wizard.
            logger.warning(
                "Email %r matches %d namespaces — using the first (%s)",
                self.email, len(data), data[0].get("nickname"),
            )
        first = data[0]
        self._namespace_id = first.get("id")
        org = first.get("org") or {}
        self._org_id = org.get("id")
        logger.info(
            "Resolved namespace id=%s org id=%s (%s)",
            self._namespace_id, self._org_id, org.get("name"),
        )

    def login(self) -> bool:
        try:
            if self._namespace_id is None:
                self._determine_namespace()
            resp = self._client.post(
                "/auth/login",
                json={
                    "email": self.email,
                    "password": self.password,
                    "namespaceId": self._namespace_id,
                },
                headers={"Content-Type": "application/json"},
            )
            logger.info("Alta login: status=%s", resp.status_code)
            if resp.status_code != 201:
                logger.error("Alta login FAILED: HTTP %s", resp.status_code)
                return False

            data = (resp.json() or {}).get("data") or {}
            token = data.get("token")
            if not token:
                logger.error("Alta login response had no data.token")
                return False

            self._token = token
            self._token_expires_at = _parse_iso(data.get("expiresAt"))
            self._identity_id = data.get("identityId")
            # The org id and the user id both live inside tokenScopeList
            # entries, not at the top level. Prefer the org id carried there;
            # fall back to the one resolved during namespace discovery.
            scope_list = data.get("tokenScopeList") or []
            for entry in scope_list:
                org = (entry or {}).get("org") or {}
                user = (entry or {}).get("user") or {}
                if org.get("id"):
                    self._org_id = org["id"]
                    if user.get("id"):
                        self._user_id = user["id"]
                    break

            logger.info(
                "Alta login SUCCESS: org=%s user=%s expires=%s",
                self._org_id, self._user_id, self._token_expires_at,
            )
            return True
        except httpx.HTTPError as e:
            logger.error("Alta login transport error: %s: %s", type(e).__name__, e)
            return False

    def _token_valid(self) -> bool:
        if not self._token:
            return False
        if self._token_expires_at is None:
            return True
        return datetime.now(UTC) < (self._token_expires_at - TOKEN_EXPIRY_SKEW)

    def _ensure_authenticated(self) -> None:
        if not self._token_valid() and not self.login():
            raise AltaAuthError("Cannot authenticate with Avigilon Alta Access")

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._ensure_authenticated()
        headers = kwargs.pop("headers", {})
        headers.setdefault("authorization", self._token)
        resp = self._client.request(method, path, headers=headers, **kwargs)
        if resp.status_code == 401:
            logger.warning("Alta returned 401 — re-authenticating")
            self._token = ""
            if self.login():
                headers["authorization"] = self._token
                resp = self._client.request(method, path, headers=headers, **kwargs)
        return resp

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        try:
            if not self.login():
                return False, "Login failed — check email/password"
            resp = self._request(
                "GET", f"/orgs/{self._org_id}/users",
                params={"order": "asc", "limit": 1},
            )
            if resp.status_code == 200:
                return True, ""
            return False, f"Probe returned HTTP {resp.status_code}"
        except (AltaAuthError, httpx.HTTPError) as e:
            return False, f"{type(e).__name__}: {e}"

    def list_users(self) -> list[dict[str, Any]]:
        """Fetch every user in the org, following pagination."""
        users: list[dict[str, Any]] = []
        offset = 0
        while True:
            resp = self._request(
                "GET", f"/orgs/{self._org_id}/users",
                params={"order": "asc", "limit": PAGE_SIZE, "offset": offset},
            )
            if resp.status_code != 200:
                logger.error("list_users: HTTP %s at offset %s", resp.status_code, offset)
                break
            body = resp.json() or {}
            page = body.get("data") or []
            users.extend(page)
            total = body.get("totalCount", len(users))
            offset += PAGE_SIZE
            if not page or len(users) >= total:
                break
        logger.info("Alta: %d users loaded", len(users))
        return users

    def get_user(self, user_id: str | int) -> dict[str, Any] | None:
        resp = self._request("GET", f"/orgs/{self._org_id}/users/{user_id}")
        if resp.status_code != 200:
            return None
        return (resp.json() or {}).get("data")

    def list_credentials(self, user_id: str | int) -> list[dict[str, Any]]:
        resp = self._request(
            "GET", f"/orgs/{self._org_id}/users/{user_id}/credentials",
            params={"limit": 1000, "offset": 0},
        )
        if resp.status_code != 200:
            logger.error("list_credentials(%s): HTTP %s", user_id, resp.status_code)
            return []
        return (resp.json() or {}).get("data") or []

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def patch_credential_dates(
        self,
        user_id: str | int,
        credential_id: str | int,
        *,
        start_date: str | None,
        end_date: str | None,
        card_number: str,
        card_format_id: int,
        is_output_enabled: bool = False,
    ) -> bool:
        """PATCH a credential's validity window.

        Alta has no explicit suspend flag — a credential is deactivated by
        moving its `endDate` into the past. The card block must be echoed
        back or the PATCH blanks the physical card, so callers pass the
        existing card number + format id through.
        """
        body: dict[str, Any] = {
            "startDate": start_date,
            "endDate": end_date,
            "eventAction": None,
            "badgeConfigId": None,
            "card": {
                "number": card_number,
                "cardFormatId": card_format_id,
                "isOutputEnabled": is_output_enabled,
            },
        }
        resp = self._request(
            "PATCH",
            f"/orgs/{self._org_id}/users/{user_id}/credentials/{credential_id}",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code in (200, 204):
            return True
        logger.error(
            "patch_credential_dates(%s/%s): HTTP %s",
            user_id, credential_id, resp.status_code,
        )
        return False

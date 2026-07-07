"""HTTP client for CDVI Atrium controllers (on-prem).

Ported from the accessgrid.com `Cdvi` Ruby service. Atrium speaks an
encrypted XML protocol over HTTP:

  1. GET  /login.xml?...&sid=<seed>      → seeds a session, returns the
     session id in a `Session=` cookie and the controller serial in the
     LOGIN/HEADER@sn attribute.
  2. POST /login.xml?...&sid=<session>   → authenticates. The username is
     RC4-encrypted with the session id, the password is sent as
     uppercased MD5(session_id + password). A successful login has
     LOGIN/HEADER@user_id != "-1".

After login, list endpoints (`users.xml`, `cards.xml`) return an
RC4-encrypted, checksummed body of the form `post_enc=<hex>&post_chk=<hex>`
that we decrypt with the session id.

This client is intentionally **read-only**. It ports only the login and
the two list endpoints — it deliberately does NOT port user create/update
(nor card writes). The sync tool must never mutate the CDVI directory.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0
# Ruby port sends a browser-like UA; Atrium's embedded web server is picky.
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

# Fixed client seed used to prime the very first login.xml GET, mirroring
# the Ruby implementation. The real session id is issued in the response.
LOGIN_SEED_SID = "1013B3842BBBA23D"

# Safety cap so a misbehaving controller can't spin pagination forever.
MAX_PAGES = 100


class CdviAuthError(Exception):
    pass


class CdviAPIError(Exception):
    pass


# ---------------------------------------------------------------------------
# Crypto helpers (RC4 / MD5 / checksum) — direct ports of the Ruby methods.
# ---------------------------------------------------------------------------


def _rc4_keystream_setup(key: str) -> list[int]:
    """RC4 key-scheduling algorithm. Returns the initialized S-box."""
    key_bytes = key.encode("latin-1")
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key_bytes[i % len(key_bytes)]) % 256
        s[i], s[j] = s[j], s[i]
    return s


def rc4_encrypt(key: str, text: str) -> str:
    """RC4-encrypt `text` under `key`, returning an uppercase hex string."""
    s = _rc4_keystream_setup(key)
    i = j = 0
    out: list[str] = []
    for byte in text.encode("latin-1"):
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        k = s[(s[i] + s[j]) % 256]
        out.append(f"{byte ^ k:02X}")
    return "".join(out)


def rc4_decrypt(key: str, encrypted_hex: str) -> str:
    """RC4-decrypt an uppercase hex string under `key` back to text."""
    s = _rc4_keystream_setup(key)
    i = j = 0
    out = bytearray()
    for y in range(0, len(encrypted_hex) - 1, 2):
        byte_value = int(encrypted_hex[y : y + 2], 16)
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        k = s[(s[i] + s[j]) % 256]
        out.append(byte_value ^ k)
    # Bytes form an XML document; decode leniently so a stray high byte
    # doesn't nuke the whole parse.
    return out.decode("utf-8", errors="replace")


def post_chk_calc(text: str) -> str:
    """Lower-16-bit sum of the plaintext bytes, as 4-char uppercase hex."""
    chk = sum(text.encode("latin-1")) & 0xFFFF
    return f"{chk:04X}"


def md5_password(session_id: str, password: str) -> str:
    return hashlib.md5((session_id + password).encode("utf-8")).hexdigest().upper()


# ---------------------------------------------------------------------------
# XML → dict, ported from Nokogiri node_to_hash (namespaces stripped).
# ---------------------------------------------------------------------------


def _localname(tag: str) -> str:
    # ElementTree renders namespaced tags as "{uri}local".
    return tag.split("}", 1)[1] if "}" in tag else tag


def node_to_hash(node: ET.Element) -> Any:
    result: dict[str, Any] = {}

    for name, value in node.attrib.items():
        result[_localname(name)] = value

    children = list(node)
    if not children:
        text = (node.text or "").strip()
        if text and not result:
            return text
        if text:
            result["_text"] = text
        return result

    for child in children:
        name = _localname(child.tag)
        child_result = node_to_hash(child)
        if name in result:
            if isinstance(result[name], list):
                result[name].append(child_result)
            else:
                result[name] = [result[name], child_result]
        else:
            result[name] = child_result
    return result


def parse_xml_to_hash(xml_string: str) -> dict[str, Any] | None:
    if not xml_string:
        return None
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError as e:
        logger.error("CDVI: failed to parse XML: %s", e)
        return None
    hashed = node_to_hash(root)
    return {_localname(root.tag): hashed}


class CdviClient:
    """Session-based encrypted-XML client for a CDVI Atrium controller."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username
        self.password = password

        self.session_id: str | None = None
        self.device_serial: str | None = None
        self.logged_in = False
        # user_id -> email, memoized across get_user_email() calls.
        self._email_cache: dict[str, str] = {}

        self._client = httpx.Client(
            verify=False,  # Atrium controllers ship self-signed certs on-prem.
            timeout=HTTP_TIMEOUT,
            headers={
                "Accept": "*/*",
                "User-Agent": HTTP_USER_AGENT,
            },
            follow_redirects=False,
        )
        logger.info("CdviClient init: base_url=%s user=%r", self.base_url, self.username)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CdviClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Low-level request + URL helpers
    # ------------------------------------------------------------------

    def _url_with_timestamp(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.base_url}{path}{sep}_={int(time.time())}"

    def _cookie_session(self, resp: httpx.Response) -> str | None:
        raw = resp.headers.get("set-cookie")
        if not raw:
            return None
        m = re.search(r"Session=([^;]+)", raw)
        if not m:
            return None
        # Strip the '-NN' suffix Atrium appends (e.g. "...-02").
        return re.sub(r"-\d{2}$", "", m.group(1))

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _login_get(self) -> bool:
        if not self.base_url:
            return False
        url = self._url_with_timestamp("/login.xml") + f"&sid={LOGIN_SEED_SID}"
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as e:
            logger.error("CDVI login_get transport error: %s", e)
            return False
        if resp.status_code != 200:
            logger.error("CDVI login_get HTTP %s", resp.status_code)
            return False

        parsed = parse_xml_to_hash(resp.text)
        login = (parsed or {}).get("LOGIN")
        if not isinstance(login, dict):
            logger.error("CDVI login_get: no LOGIN element")
            return False
        error = login.get("ERROR")
        if isinstance(error, str) and error.strip() not in ("", "0"):
            logger.error("CDVI login_get error node: %s", error)
            return False

        self.session_id = self._cookie_session(resp)
        header = login.get("HEADER")
        serial = header.get("sn") if isinstance(header, dict) else None
        if self.session_id and serial:
            self.device_serial = serial.strip()
            return True
        return False

    def login(self) -> bool:
        if not self._login_get():
            return False

        url = self._url_with_timestamp("/login.xml") + f"&sid={self.session_id}"
        body = {
            "cmd": "login",
            "login_user": rc4_encrypt(self.session_id or "", self.username),
            "login_pass": md5_password(self.session_id or "", self.password),
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": f"Session={self.session_id}",
        }
        try:
            resp = self._client.post(url, data=body, headers=headers)
        except httpx.HTTPError as e:
            logger.error("CDVI login transport error: %s", e)
            return False
        if resp.status_code != 200:
            logger.error("CDVI login HTTP %s", resp.status_code)
            return False

        # The controller may rotate the session id on login.
        rotated = self._cookie_session(resp)
        if rotated:
            self.session_id = rotated

        parsed = parse_xml_to_hash(resp.text)
        login = (parsed or {}).get("LOGIN")
        if not isinstance(login, dict):
            logger.error("CDVI login: no LOGIN element in response")
            return False
        error = login.get("ERROR")
        if isinstance(error, str) and error.strip() not in ("", "0"):
            logger.error("CDVI login error node: %s", error)
            return False

        header = login.get("HEADER")
        user_id = header.get("user_id") if isinstance(header, dict) else None
        if user_id is not None and str(user_id) != "-1":
            self.logged_in = True
            logger.info("CDVI login SUCCESS: serial=%s", self.device_serial)
            return True
        logger.error("CDVI login FAILED: user_id=%r", user_id)
        return False

    def _ensure_authenticated(self) -> None:
        if not self.logged_in and not self.login():
            raise CdviAuthError("Cannot authenticate with CDVI Atrium")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        try:
            if not self.login():
                return False, "Login failed — check base URL / username / password"
            return True, ""
        except httpx.HTTPError as e:
            return False, f"{type(e).__name__}: {e}"

    def _get_encrypted(self, path: str) -> dict[str, Any] | None:
        """GET a list endpoint, decrypt the body, and parse it to a hash."""
        cookie = {"Cookie": f"Session={self.session_id}"} if self.session_id else {}
        try:
            resp = self._client.get(self.base_url + path, headers=cookie)
        except httpx.HTTPError as e:
            logger.error("CDVI GET %s transport error: %s", path, e)
            return None
        if resp.status_code != 200:
            logger.error("CDVI GET %s HTTP %s", path, resp.status_code)
            return None
        xml_data = self.decrypt_payload(resp.text)
        if not xml_data:
            return None
        return parse_xml_to_hash(xml_data)

    def decrypt_payload(self, body: str) -> str | None:
        """Decrypt a `post_enc=..&post_chk=..` body; pass plain XML through."""
        if body is None:
            return None
        if "post_enc=" not in body:
            return body
        try:
            enc = body.split("post_enc=", 1)[1]
            encrypted, _, chk_post = enc.partition("&post_chk=")
        except (IndexError, ValueError):
            return None
        if not self.session_id or not chk_post:
            return None
        decrypted = rc4_decrypt(self.session_id, encrypted)
        chk_calc = int(post_chk_calc(decrypted), 16) & 0xFFFF
        try:
            if int(chk_post, 16) != chk_calc:
                logger.error("CDVI decrypt: checksum mismatch")
                return None
        except ValueError:
            return None
        return decrypted

    def _paginate(self, path_for_page) -> list[dict[str, Any]]:
        """Collect a repeated child node across pages until a page is empty."""
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            parsed = self._get_encrypted(self._path_ts(path_for_page(page)))
            page_items = self._extract_records(parsed)
            if not page_items:
                break
            items.extend(page_items)
            # A short page means we've reached the end.
            if len(page_items) < PAGE_RECORDS_HINT:
                break
        return items

    def _path_ts(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}_={int(time.time())}&sid={self.session_id}"

    @staticmethod
    def _extract_records(parsed: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Pull USER/CARD records out of a parsed list response.

        Atrium wraps rows in a container element; the row tag repeats. We
        find the first list-valued (or lone dict) child that looks like a
        record and normalize it to a list of dicts.
        """
        if not parsed:
            return []
        # parsed is {"<ROOT>": {...}}. Look one level in for USER/CARD.
        root = next(iter(parsed.values()))
        if not isinstance(root, dict):
            return []
        for key in ("USER", "CARD"):
            if key in root:
                value = root[key]
                if isinstance(value, list):
                    return [v for v in value if isinstance(v, dict)]
                if isinstance(value, dict):
                    return [value]
        return []

    def list_users(self) -> list[dict[str, Any]]:
        self._ensure_authenticated()
        users = self._paginate(
            lambda page: f"/users.xml?page_nb={page}&user_filter="
        )
        logger.info("CDVI: %d users loaded", len(users))
        return users

    def list_cards(self) -> list[dict[str, Any]]:
        self._ensure_authenticated()
        cards = self._paginate(
            lambda page: f"/cards.xml?page_nb={page}&card_number="
        )
        logger.info("CDVI: %d cards loaded", len(cards))
        return cards

    def _encrypt_payload(self, payload: str) -> str | None:
        """Wrap a plaintext body as `post_enc=..&post_chk=..` for the SDK.

        Used only for read commands (cmd='read'); the adapter never issues
        writes. Returns None if there is no session to key the cipher with.
        """
        if not self.session_id:
            return None
        return f"post_enc={rc4_encrypt(self.session_id, payload)}&post_chk={post_chk_calc(payload)}"

    def get_user_email(self, user_id: str | int) -> str:
        """Read a user's email from the SDK `cfg2` record (attribute email5).

        Email is not present in users.xml; it lives in a separate per-user
        SDK record. Users without an email have obj_status="free" and no
        email5 attribute, so we return "". Result is memoized per user.
        Any transport/parse failure degrades to "" rather than raising.
        """
        uid = str(user_id)
        if uid in self._email_cache:
            return self._email_cache[uid]

        self._ensure_authenticated()
        xml = (
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
            "<SDK xmlns='https://www.cdvi.ca/'><RECORDS>"
            f"<REC trans_id='1' cmd='read' sernum='{self.device_serial}' "
            f"type='user' id='{uid}' rec='cfg2'></REC>"
            "</RECORDS></SDK>"
        )
        email = ""
        body = self._encrypt_payload(xml)
        if body is not None:
            try:
                resp = self._client.post(
                    f"{self.base_url}/sdk.xml?_={int(time.time())}&sid={self.session_id}",
                    content=body,
                    headers={"Cookie": f"Session={self.session_id}"},
                )
                if resp.status_code == 200:
                    email = self._extract_email(self.decrypt_payload(resp.text))
            except httpx.HTTPError as e:
                logger.warning("CDVI get_user_email(%s) transport error: %s", uid, e)

        self._email_cache[uid] = email
        return email

    @staticmethod
    def _extract_email(sdk_xml: str | None) -> str:
        parsed = parse_xml_to_hash(sdk_xml) if sdk_xml else None
        rec = (((parsed or {}).get("SDK") or {}).get("RECORDS") or {}).get("REC")
        if isinstance(rec, list):
            rec = rec[0] if rec else {}
        data = (rec or {}).get("DATA") if isinstance(rec, dict) else None
        if isinstance(data, list):
            data = data[0] if data else {}
        return (data.get("email5") or "").strip() if isinstance(data, dict) else ""


# Atrium pages list responses; a full page is typically this many rows.
# Used only as a "keep paginating" hint — correctness doesn't depend on the
# exact value because we also stop as soon as a page returns zero rows.
PAGE_RECORDS_HINT = 50

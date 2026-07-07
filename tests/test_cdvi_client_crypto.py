"""CDVI crypto / XML-parsing parity with the Ruby implementation.

No network: these exercise the pure helpers ported from the Ruby `Cdvi`
service — RC4, the payload checksum, MD5 auth hashing, the encrypted-body
decrypt path, and Nokogiri-style XML→hash conversion.

The RUBY_* vectors below are ground truth captured from the real Ruby
service (accessgrid.com app/services/cdvi.rb). Regenerate them with:

    ruby scripts/cdvi_crypto_vectors.rb

The Python port must reproduce every one of these byte-for-byte.
"""

from __future__ import annotations

import hashlib

import pytest

from agsync.lib.pacs.cdvi.adapter import (
    TRIGGER_PATTERN,
    _decode_card_number,
    _trigger_platform,
)
from agsync.lib.pacs.cdvi.client import (
    CdviClient,
    md5_password,
    parse_xml_to_hash,
    post_chk_calc,
    rc4_decrypt,
    rc4_encrypt,
)

SESSION = "1013B3842BBBA23D"


def _client() -> CdviClient:
    # __init__ builds an httpx.Client but makes no network call.
    return CdviClient(base_url="https://ctrl.test", username="u", password="p")


# --- Ground-truth vectors from the Ruby service --------------------------

# (key, plaintext) -> uppercase hex
RUBY_RC4 = [
    (SESSION, "auston", "32EF976ADA2D"),
    (
        SESSION,
        "cmd=login&user=auston&pass=secret",
        "30F78023D92C24EB1092DE5E332CA1A3EA56644EBB404112757434FAD228A109E6",
    ),
    ("Key", "Plaintext", "BBF316E8D940AF0AD3"),  # also the canonical RC4 vector
    ("Wiki", "pedia", "1021BF0420"),
    (
        SESSION,
        "T_card_cmd=add&T_card_name=AccessGrid+test",
        "07C5877FC7271CE113D0964C323ABA96C0467153B1395F126B6234C8D428B61FE1E0659EFBA3D1E97C54",
    ),
    (SESSION, "", ""),
]

# text -> 4-char uppercase hex checksum
RUBY_POST_CHK = [
    ("AB", "0083"),
    ("auston", "029A"),
    ("<USERS><USER id='5'/></USERS>", "07DC"),
    ("Plaintext", "03B9"),
    ("", "0000"),
]

# (session, password) -> upper(md5(session + password))
RUBY_MD5 = [
    (SESSION, "secret", "783DEABCE62EA6F63C149F4C4D33CF9B"),
    (SESSION, "hunter2", "36D42636EFA9C2A3ADCF0B8F49952FF2"),
    ("SESSION", "secret", "97699CA225AB5665AA16284E8ABE2CD4"),
]

# encrypted body (session=SESSION) -> decrypted plaintext
RUBY_DECRYPT = [
    (
        "post_enc=30FB967AEA202EE643D0CE41332AF9E4FC4462458A0F554E3235&post_chk=096C",
        "card_cmd=delete&card_id=42",
    ),
    (
        "post_enc=6FCFB75BE7107DBE2BE7EE7F7637F8FFB8103701B3080C54476A70AE9875EF43C7F452A5CCB6&post_chk=0A82",
        "<USERS><USER id='5' fn='Amy'/></USERS>",
    ),
]

# Ruby convert_card_data(site_code, card_number) -> encoded hex (lowercase!)
RUBY_CONVERT = [
    (69, 42069, "000000000045a455"),
    (0, 0, "0000000000000000"),
    (1, 1, "0000000000010001"),
    (255, 65535, "0000000000ffffff"),
    (12, 3456, "00000000000c0d80"),
]


# --- RC4 ----------------------------------------------------------------


@pytest.mark.parametrize(("key", "text", "expected_hex"), RUBY_RC4)
def test_rc4_encrypt_matches_ruby(key, text, expected_hex):
    assert rc4_encrypt(key, text) == expected_hex


@pytest.mark.parametrize(("key", "text", "expected_hex"), RUBY_RC4)
def test_rc4_decrypt_matches_ruby(key, text, expected_hex):
    assert rc4_decrypt(key, expected_hex) == text


# --- checksum + password hashing ----------------------------------------


@pytest.mark.parametrize(("text", "expected"), RUBY_POST_CHK)
def test_post_chk_calc_matches_ruby(text, expected):
    assert post_chk_calc(text) == expected


@pytest.mark.parametrize(("session", "password", "expected"), RUBY_MD5)
def test_md5_password_matches_ruby(session, password, expected):
    assert md5_password(session, password) == expected
    # sanity: it really is upper(md5(session + password))
    assert expected == hashlib.md5((session + password).encode()).hexdigest().upper()


# --- decrypt_payload (the read path controllers use for list endpoints) --


@pytest.mark.parametrize(("body", "plaintext"), RUBY_DECRYPT)
def test_decrypt_payload_matches_ruby(body, plaintext):
    c = _client()
    c.session_id = SESSION
    assert c.decrypt_payload(body) == plaintext
    c.close()


def test_decrypt_payload_rejects_bad_checksum():
    c = _client()
    c.session_id = SESSION
    plaintext = "<USERS/>"
    body = f"post_enc={rc4_encrypt(c.session_id, plaintext)}&post_chk=FFFF"
    assert c.decrypt_payload(body) is None
    c.close()


def test_decrypt_payload_passes_plain_xml_through():
    c = _client()
    assert c.decrypt_payload("<LOGIN><ERROR>0</ERROR></LOGIN>") == "<LOGIN><ERROR>0</ERROR></LOGIN>"
    c.close()


# --- XML → hash (Nokogiri node_to_hash parity) ---------------------------


def test_parse_users_xml_produces_user_list():
    xml = (
        "<USERS>"
        "<USER id='5' fn='Amy' ln='Hyatt' en='1' email='amy@example.com'/>"
        "<USER id='6' fn='Bob' ln='Lee' en='1'/>"
        "</USERS>"
    )
    parsed = parse_xml_to_hash(xml)
    users = parsed["USERS"]["USER"]
    assert isinstance(users, list)
    assert users[0] == {"id": "5", "fn": "Amy", "ln": "Hyatt", "en": "1", "email": "amy@example.com"}
    assert users[1]["fn"] == "Bob"


def test_parse_single_user_is_dict_not_list():
    parsed = parse_xml_to_hash("<USERS><USER id='5' fn='Amy'/></USERS>")
    assert parsed["USERS"]["USER"] == {"id": "5", "fn": "Amy"}


def test_parse_login_header_attributes():
    xml = "<LOGIN><ERROR>0</ERROR><HEADER sn='ABC123' user_id='42'/></LOGIN>"
    login = parse_xml_to_hash(xml)["LOGIN"]
    assert login["ERROR"] == "0"
    assert login["HEADER"]["sn"] == "ABC123"
    assert login["HEADER"]["user_id"] == "42"


def test_parse_bad_xml_returns_none():
    assert parse_xml_to_hash("<UNCLOSED") is None


# --- card number decode (reverse of Ruby convert_card_data) --------------


@pytest.mark.parametrize(("site_code", "card_number", "encoded"), RUBY_CONVERT)
def test_decode_reverses_ruby_convert_card_data(site_code, card_number, encoded):
    # We decode what Ruby's convert_card_data encodes — exact round-trip
    # against the real encoded strings (note: Ruby emits lowercase hex).
    assert _decode_card_number(encoded) == (str(site_code), str(card_number))


def test_decode_card_number_short_hex():
    assert _decode_card_number("45A455") == ("69", "42069")


def test_decode_card_number_invalid():
    assert _decode_card_number("nope") == ("", "")
    assert _decode_card_number("") == ("", "")


# --- display-name trigger pattern ----------------------------------------


def test_trigger_pattern_matches_variants():
    for name in ("[accessgrid]", "x [accessgrid-apple] y", "[ACCESSGRID-Android]"):
        assert TRIGGER_PATTERN.search(name) is not None


def test_trigger_pattern_rejects_non_matches():
    for name in ("accessgrid", "[accessgrid-windows]", "[access grid]", ""):
        assert TRIGGER_PATTERN.search(name) is None


def test_trigger_platform_capture():
    assert _trigger_platform("Amy [accessgrid-apple]") == "apple"
    assert _trigger_platform("[accessgrid-ANDROID]") == "android"
    assert _trigger_platform("[accessgrid]") is None
    assert _trigger_platform("no marker") is None

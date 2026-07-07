"""CDVI crypto / XML-parsing parity with the Ruby implementation.

No network: these exercise the pure helpers ported from the Ruby `Cdvi`
service — RC4, the payload checksum, MD5 auth hashing, the encrypted-body
decrypt path, and Nokogiri-style XML→hash conversion.
"""

from __future__ import annotations

from agsync.lib.pacs.cdvi.adapter import _decode_card_number
from agsync.lib.pacs.cdvi.client import (
    CdviClient,
    md5_password,
    parse_xml_to_hash,
    post_chk_calc,
    rc4_decrypt,
    rc4_encrypt,
)


def _client() -> CdviClient:
    # __init__ builds an httpx.Client but makes no network call.
    return CdviClient(base_url="https://ctrl.test", username="u", password="p")


# --- RC4 (standard test vectors — proves parity with the Ruby port) ------


def test_rc4_known_answer_vector():
    # Canonical RC4 test vector: RC4("Key", "Plaintext") = BBF316E8D940AF0AD3.
    assert rc4_encrypt("Key", "Plaintext") == "BBF316E8D940AF0AD3"


def test_rc4_second_known_vector():
    assert rc4_encrypt("Wiki", "pedia") == "1021BF0420"


def test_rc4_round_trip():
    key = "1013B3842BBBA23D"
    text = "cmd=login&user=auston"
    assert rc4_decrypt(key, rc4_encrypt(key, text)) == text


# --- checksum + password hashing ----------------------------------------


def test_post_chk_calc_lower_16_bits():
    # 'A'(65) + 'B'(66) = 131 = 0x0083.
    assert post_chk_calc("AB") == "0083"


def test_md5_password_is_upper_hex_of_session_plus_password():
    import hashlib

    expected = hashlib.md5(b"SESSIONsecret").hexdigest().upper()
    assert md5_password("SESSION", "secret") == expected


# --- decrypt_payload (the read path controllers use for list endpoints) --


def test_decrypt_payload_round_trips_encrypted_body():
    c = _client()
    c.session_id = "1013B3842BBBA23D"
    plaintext = "<USERS><USER id='5' fn='Amy'/></USERS>"
    body = f"post_enc={rc4_encrypt(c.session_id, plaintext)}&post_chk={post_chk_calc(plaintext)}"
    assert c.decrypt_payload(body) == plaintext
    c.close()


def test_decrypt_payload_rejects_bad_checksum():
    c = _client()
    c.session_id = "1013B3842BBBA23D"
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
        "<USER id='5' fn='Amy' ln='Hyatt' en='1' custom_large1='yes'/>"
        "<USER id='6' fn='Bob' ln='Lee' en='1'/>"
        "</USERS>"
    )
    parsed = parse_xml_to_hash(xml)
    users = parsed["USERS"]["USER"]
    assert isinstance(users, list)
    assert users[0] == {"id": "5", "fn": "Amy", "ln": "Hyatt", "en": "1", "custom_large1": "yes"}
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


def test_decode_card_number_splits_site_and_card():
    # 69 << 16 | 42069 = 0x45A455.
    assert _decode_card_number("000000000045A455") == ("69", "42069")


def test_decode_card_number_short_hex():
    assert _decode_card_number("45A455") == ("69", "42069")


def test_decode_card_number_invalid():
    assert _decode_card_number("nope") == ("", "")
    assert _decode_card_number("") == ("", "")

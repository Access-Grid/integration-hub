"""The DMP probe runs the hub's own client and adapter, so most of its
correctness is theirs. These guard the parts the probe adds: that it redacts
the columns that carry a person's identity or card number, and that its notion
of the email field and the DBISAM live-record oracle stay in step with the
adapter and client.
"""

import importlib.util
from pathlib import Path

from agsync.lib.pacs.dmp import adapter

_spec = importlib.util.spec_from_file_location(
    "dmp_probe", Path(__file__).parent.parent / "scripts" / "dmp_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_redacts_identity_and_card_columns():
    # Name, card code, PIN, department and the email User Field are the columns
    # that carry a person — each must come back as a shape, never the value.
    for col, value in [
        ("NAME", "Tyler Snyder"),
        ("FIRST_NAME", "Tyler"),
        ("CODE", "13777"),
        ("CODE_2", "99"),
        ("USER_PIN", "1234"),
        ("DEPARTMENT", "Facilities"),
        ("U_FIELD1", "tyler@example.com"),
    ]:
        out = probe.redact(value, col)
        assert out != value and out.startswith("<str len="), (col, out)


def test_keeps_counts_flags_and_column_names():
    # Status, flags, numbers and dates are the point of the report; they stay.
    assert probe.redact("active", "STATUS") == "active"
    assert probe.redact(171, "USER_NUM") == 171
    assert probe.redact(True, "ACTIVE") is True
    assert probe.redact("2026-07-21", "LAST_PNL_CMP") == "2026-07-21"


def test_redact_labels_an_email_shape():
    assert probe.redact("a@b.co", "U_FIELD1") == "<str len=6 email>"
    assert probe.redact("13777", "CODE") == "<str len=5 digits>"


def test_email_shape_matches_an_address_not_a_name():
    assert probe._EMAIL_SHAPE.search("jane.doe@acme.com")
    assert not probe._EMAIL_SHAPE.search("Jane Doe")


def test_default_email_field_tracks_the_adapter():
    assert adapter.DEFAULT_EMAIL_FIELD in probe._EMAIL_FIELD_CANDIDATES
    assert probe._EMAIL_FIELD_CANDIDATES[0] == adapter.DEFAULT_EMAIL_FIELD


def test_pii_regex_covers_the_fields_the_adapter_reads():
    # The adapter reads NAME, DEPARTMENT, CODE and the email User Field off each
    # row; every one must be a column the redactor hides.
    for col in ("NAME", "DEPARTMENT", "CODE", adapter.DEFAULT_EMAIL_FIELD):
        assert probe._PII_KEY.search(col), col


def test_slots_lists_every_record_header_without_names_or_card_numbers(tmp_path):
    import json
    from datetime import datetime, timedelta

    from .dmp_dbisam import user, users_dat

    compared = datetime(2026, 11, 16, 9)
    (tmp_path / "Users.dat").write_bytes(users_dat([
        (0x00, user(7, "13777", "TYLER SNYDER", compared, "tyler@x.co")),
        (0x05, user(8, "13778", "JANE DOE", compared - timedelta(days=40))),  # an old ghost
    ], live=1))
    p = probe.Probe(str(tmp_path), "k", "slots", "")
    report = p.run_slots()["slots"]
    assert report["header_live_records"] == 1 and report["record_header_bytes"] == 25
    assert report["hub_reads_as_current"] == 1 and report["told_apart_by"] == "status"
    assert report["status_byte_by_class"] == {"current": {"0x00": 1}, "deleted": {"0x05": 1}}
    first, second = report["slots"]
    assert (first["user_num"], first["status_byte"], first["header_1_4"], first["header_5_8"]) == (7, "0x00", 1, 1)
    assert second["hub_reads_as"].startswith("deleted") and second["header_after_checksum"] == ""
    text = json.dumps(report, default=str)
    for secret in ("13777", "13778", "TYLER", "JANE", "tyler@x.co"):
        assert secret not in text, secret

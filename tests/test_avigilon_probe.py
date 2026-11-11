"""The probe carries its own copy of what the Unity adapter reads and writes,
so the exe can run without the rest of the hub. These keep the copy honest."""

import importlib.util
from pathlib import Path

from agsync.lib.pacs.avigilon import adapter, client

_spec = importlib.util.spec_from_file_location(
    "avigilon_probe", Path(__file__).parent.parent / "scripts" / "avigilon_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_probe_expects_the_fields_the_client_writes():
    assert probe.TOKEN_FORM_FIELDS == client.TOKEN_FORM_FIELDS
    assert probe.TOKEN_FORM_DATES == client.TOKEN_FORM_DATES
    assert probe._PREFIXES == client._PREFIXES


def test_probe_reads_statuses_dates_and_searches_as_the_adapter_does():
    assert probe.STATUS_CODES == client._STATUS_CODES
    assert probe.DATE_FORMATS == adapter._DATE_FORMATS
    assert probe._XML_SEARCH == client.XML_IDENTITY_SEARCH
    assert probe._EMBOSSED_SEARCH == client.EMBOSSED_SEARCH
    assert probe._INTERNAL_SEARCH == client.INTERNAL_SEARCH
    assert probe._XML_PAGE == client.XML_PAGE_SIZE

"""Resolving which credential technology a card template issues.

The answer decides which direction the whole integration runs in: Seos
means AccessGrid mints credentials and the adapter writes them into the
PACS, DESFire means the PACS is the source of truth and nothing is ever
written. Getting it wrong is silent — the sync simply does the other thing.
"""

from __future__ import annotations

from types import SimpleNamespace

from agsync.ag import template_protocol


class _Console:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error
        self.asked: list[str] = []

    def read_template(self, template_id):
        self.asked.append(template_id)
        if self._error:
            raise self._error
        return self._result


def _client(result=None, error=None):
    return SimpleNamespace(console=_Console(result, error))


def _tpl(protocol):
    return SimpleNamespace(protocol=protocol)


# --- a single template ---------------------------------------------------


def test_a_single_template_reports_its_protocol():
    assert template_protocol(_client(_tpl("seos")), "tpl-1") == "seos"


def test_case_and_padding_do_not_matter():
    assert template_protocol(_client(_tpl("  SEOS ")), "tpl-1") == "seos"


def test_a_template_declaring_nothing_is_undetermined():
    assert template_protocol(_client(_tpl("")), "tpl-1") == ""


# --- a pair --------------------------------------------------------------


def test_a_pair_agreeing_reports_that_protocol():
    """The endpoint answers a pair id with a list, one entry per platform."""
    assert template_protocol(_client([_tpl("seos"), _tpl("seos")]), "pair-1") == "seos"


def test_a_pair_that_disagrees_is_undetermined(caplog):
    """No single answer exists, and picking one silently chooses a
    direction for the whole integration."""
    with caplog.at_level("WARNING"):
        assert template_protocol(
            _client([_tpl("seos"), _tpl("desfire")]), "pair-1",
        ) == ""
    assert "disagree about protocol" in caplog.text


def test_a_half_declaring_nothing_does_not_veto_the_other():
    """Not a vote against; the halves that do declare still have to agree."""
    assert template_protocol(_client([_tpl("seos"), _tpl("")]), "pair-1") == "seos"


def test_a_pair_declaring_nothing_at_all_is_undetermined():
    assert template_protocol(_client([_tpl(""), _tpl("")]), "pair-1") == ""


def test_a_desfire_pair_reports_desfire():
    assert template_protocol(
        _client([_tpl("desfire"), _tpl("desfire")]), "pair-1",
    ) == "desfire"


# --- failure -------------------------------------------------------------


def test_an_unreachable_api_is_undetermined_not_a_guess(caplog):
    """Undetermined means the read-only direction downstream. Reading it as
    the writing direction would create credentials in a customer's PACS on
    the strength of a value we never established."""
    with caplog.at_level("WARNING"):
        assert template_protocol(
            _client(error=RuntimeError("Card template not found")), "tpl-1",
        ) == ""
    assert "Could not read card template" in caplog.text

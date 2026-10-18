"""What a card template is, read before anything is issued from it.

Both facts here change how a credential has to be written into a PACS that
receives them, and both used to be discoverable only *after* provisioning,
by looking at what came back:

* a **pair** produces two passes, one per platform — and on HID that means
  two card numbers and two of a cardholder's three slots; and
* on **DESFire or SmartTap** both halves of a pair share one card number, so
  a pair consumes one slot rather than two.

AccessGrid states both up front. `GET /v1/console/card-templates/{id}`
answers with a `templates` list for a pair and a single template otherwise,
which is the only thing distinguishing them, so it is pinned here.
"""

from __future__ import annotations

from types import SimpleNamespace

from agsync.ag import TemplateShape, read_template_shape, template_protocol


def _client(result=None, raises=None):
    def _read(template_id):
        if raises is not None:
            raise raises
        return result
    return SimpleNamespace(console=SimpleNamespace(read_template=_read))


def _template(protocol="seos", platform="apple"):
    return SimpleNamespace(protocol=protocol, platform=platform)


# =====================================================================
# Pairs
# =====================================================================


def test_a_list_is_a_pair():
    """The SDK returns a list only when the response carried `templates`."""
    shape = read_template_shape(
        _client([_template(platform="apple"), _template(platform="android")]), "t"
    )

    assert shape.paired is True
    assert shape.known is True
    assert shape.platforms == ("apple", "android")


def test_a_single_template_is_not_a_pair():
    shape = read_template_shape(_client(_template()), "t")

    assert shape.paired is False
    assert shape.platforms == ("apple",)


def test_a_one_element_list_is_not_treated_as_a_pair():
    """A pair has two halves. One template in a list is still one template,
    and reserving two card slots for it would deny a pass to a cardholder
    who has three."""
    shape = read_template_shape(_client([_template()]), "t")

    assert shape.paired is False


# =====================================================================
# How many PACS slots one issue needs
# =====================================================================


def test_an_hid_pair_needs_two_slots():
    shape = read_template_shape(
        _client([_template("seos", "apple"), _template("seos", "android")]), "t"
    )

    assert shape.slots_per_issue == 2


def test_a_desfire_pair_needs_one_slot():
    """Both halves carry the same card number, so the PACS holds one card."""
    shape = read_template_shape(
        _client([_template("desfire", "apple"), _template("desfire", "android")]),
        "t",
    )

    assert shape.slots_per_issue == 1


def test_a_smarttap_pair_needs_one_slot():
    shape = read_template_shape(
        _client([_template("smarttap", "apple"), _template("smarttap", "android")]),
        "t",
    )

    assert shape.slots_per_issue == 1


def test_a_single_template_needs_one_slot():
    assert read_template_shape(_client(_template()), "t").slots_per_issue == 1


# =====================================================================
# When it cannot be read
# =====================================================================


def test_an_unreadable_template_is_reported_as_unknown_not_as_a_default():
    """So callers can decline to branch rather than branch on a guess."""
    shape = read_template_shape(_client(raises=RuntimeError("502")), "t")

    assert shape.known is False
    assert shape.protocol == ""
    assert shape.paired is False


def test_an_unknown_shape_reserves_one_slot_not_two():
    """Over-reserving would stop a pass being issued at all."""
    assert TemplateShape().slots_per_issue == 1


def test_a_lookup_failure_never_raises(caplog):
    """A sync cycle must not stop because a template lookup did."""
    with caplog.at_level("WARNING"):
        read_template_shape(_client(raises=RuntimeError("boom")), "t")

    assert "Could not read card template" in caplog.text


# =====================================================================
# The old helper still answers the same question
# =====================================================================


def test_the_protocol_helper_reads_through_the_same_call():
    assert template_protocol(_client(_template("seos")), "t") == "seos"


def test_the_protocol_helper_is_empty_when_unreadable():
    assert template_protocol(_client(raises=RuntimeError("x")), "t") == ""


def test_the_protocol_comes_from_either_half_of_a_pair():
    shape = read_template_shape(
        _client([_template("", "apple"), _template("seos", "android")]), "t"
    )

    assert shape.protocol == "seos"

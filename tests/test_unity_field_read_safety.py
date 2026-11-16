"""Unread Unity tokens hold field updates until the matching token is read again."""

import copy

import pytest

from agsync.sync import tracking

from .test_unity_spec_end_to_end import _desfire, _seos
from .test_unity_spec_end_to_end import mailed as _mailed
from .test_unity_spec_end_to_end import world as _world

mailed, world = _mailed, _world


@pytest.mark.parametrize("protocol", ["desfire", "seos"])
@pytest.mark.parametrize("pair", [False, True])
def test_unread_tokens_hold_field_changes_and_recovery_syncs_them(world, mailed, protocol, pair):
    unity, ag, cycle = _seos(world, pair) if protocol == "seos" else _desfire(world, pair)
    cycle()
    ag.install(ag.only_pass())
    cycle()
    before = copy.deepcopy(unity.tokens), tracking.all_tracked(), list(ag.calls)
    unity.identities[0].update(plasecFname="James", plasecidentityEmailaddress="james@new.example.com",
                               plasecidentityPhone="", plasecidentityTitle="", plasecidentityDepartment="")
    # Both wire formats fail, so the snapshot has a person but no token read.
    unity.fail["/identities/a/tokens.json"] = 503
    unity.fail["/identities/a/tokens.xml"] = 503
    cycle()
    cycle()
    assert (unity.tokens, tracking.all_tracked(), ag.calls) == before
    assert mailed == []  # An unread non-Seos token must not raise a Seos email alert.

    unity.fail.clear()
    cycle()
    changes = {k: v for verb, _, params in ag.calls[len(before[2]):] if verb == "update"
               for k, v in params.items()}
    assert changes == {"full_name": "James Cooper", "phone_number": "", "title": "", "department": "",
                       **({"email": "james@new.example.com"} if protocol == "desfire" else {})}
    [row] = tracking.all_tracked()
    assert (row.last_synced_full_name, row.last_synced_email, row.last_synced_phone,
            row.last_synced_title, row.last_synced_department) == ("James Cooper", "james@new.example.com", "", "", "")
    assert len(mailed) == (1 if protocol == "seos" else 0)
    calls = list(ag.calls)
    cycle()
    assert ag.calls == calls

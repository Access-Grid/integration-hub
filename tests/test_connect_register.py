"""URI-scheme registration, and the macOS quirk that makes it non-obvious.

Windows gets the URL as `%1` on the command line. macOS does not: Launch
Services sends a `kAEGetURL` Apple Event, so the handler has to be something
that can receive one — hence an AppleScript applet rather than a wrapper
around the executable. These tests pin the generated script, because a
malformed one only fails at `osacompile` time on a developer's Mac.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agsync.connect.register import (
    _applescript_quote,
    _applescript_source,
    _executable,
)


def _command_for(parts: list[str]) -> str:
    return _applescript_quote(shlex.join(parts))


# --- the script we generate ---------------------------------------------


def test_command_is_a_single_applescript_literal():
    # The regression this exists for: joining the parts as separate literals
    # produces `"a" "b" "c"`, which AppleScript rejects as a syntax error.
    source = _applescript_source(_command_for(["/usr/bin/python3", "-m", "agsync", "connect"]))
    command_line = next(ln for ln in source.splitlines() if "do shell script" in ln)
    body = command_line.split("do shell script", 1)[1]
    # One literal for the command, one for the separating space.
    assert body.count('"') == 4
    assert '" "' in body


def test_handler_receives_the_url():
    source = _applescript_source(_command_for(["/bin/echo"]))
    assert "on open location this_URL" in source
    # `quoted form` so a URL with shell metacharacters cannot break out.
    assert "quoted form of this_URL" in source


def test_paths_with_spaces_survive_both_quoting_layers():
    command = _command_for(["/Applications/My App/python", "-m", "agsync", "connect"])
    # shlex protects the shell, the outer literal protects AppleScript.
    assert "'/Applications/My App/python'" in command
    assert command.startswith('"') and command.endswith('"')


def test_embedded_quotes_are_escaped():
    assert _applescript_quote('say "hi"') == '"say \\"hi\\""'
    assert _applescript_quote("back\\slash") == '"back\\\\slash"'


def test_executable_reenters_this_cli():
    program, leading = _executable()
    assert Path(program).name
    assert leading[-1] == "connect"


# --- and that macOS actually accepts it ---------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="osacompile is macOS-only")
def test_generated_script_compiles(tmp_path):
    source = tmp_path / "handler.applescript"
    source.write_text(
        _applescript_source(_command_for([sys.executable, "-m", "agsync", "connect"])),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["/usr/bin/osacompile", "-o", str(tmp_path / "Probe.app"), str(source)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr

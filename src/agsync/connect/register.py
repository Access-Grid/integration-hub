"""Register (and remove) the ``agconnect://`` URI scheme.

A web page cannot start a local process, and the service cannot put a window
on the operator's desktop, so the OS is the bridge: the wizard renders an
``agconnect://`` link and the OS hands it to this executable. Registration is
per-user — no elevation, and nothing machine-wide to clean up.

Windows writes the standard ``HKCU\\Software\\Classes`` protocol handler,
where the URL arrives as ``%1`` on the command line.

macOS does not work that way. Launch Services delivers a URL as a
``kAEGetURL`` Apple Event, not as an argument, so an app bundle wrapping a
plain executable is launched with *no arguments at all* and the URL is
lost. The bundle therefore has to be something that can receive that event,
which is why we compile a small AppleScript applet with an
``on open location`` handler and let it shell back into this CLI.
"""

from __future__ import annotations

import logging
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .protocol import URI_SCHEME

logger = logging.getLogger(__name__)

_MAC_APP_NAME = "AG Connect.app"
_LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks"
    "/LaunchServices.framework/Support/lsregister"
)


def _executable() -> tuple[str, list[str]]:
    """(program, leading args) that re-enter this CLI's `connect` command."""
    if getattr(sys, "frozen", False):
        # PyInstaller one-file build: the exe *is* the CLI.
        return sys.executable, ["connect"]
    return sys.executable, ["-m", "agsync", "connect"]


# -- Windows -----------------------------------------------------------------


def _register_windows() -> str:
    import winreg

    program, leading = _executable()
    command = " ".join(
        [f'"{program}"', *[f'"{a}"' for a in leading], '"%1"']
    )
    root = rf"Software\Classes\{URI_SCHEME}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"URL:{URI_SCHEME} Protocol")
        # Presence of this empty value is what marks the key as a protocol.
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, rf"{root}\shell\open\command"
    ) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    return f"HKCU\\{root} -> {command}"


def _unregister_windows() -> str:
    import winreg

    root = rf"Software\Classes\{URI_SCHEME}"
    for subkey in (rf"{root}\shell\open\command", rf"{root}\shell\open", rf"{root}\shell", root):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
        except FileNotFoundError:
            pass
    return f"Removed HKCU\\{root}"


# -- macOS -------------------------------------------------------------------


def _mac_app_dir() -> Path:
    return Path.home() / "Applications" / _MAC_APP_NAME


def _register_macos() -> str:
    program, leading = _executable()
    # Two layers of quoting: shlex for the shell that `do shell script` runs,
    # then AppleScript's own string literal around the whole command.
    command = _applescript_quote(
        shlex.join([program, *leading])
    )

    app = _mac_app_dir()
    app.parent.mkdir(parents=True, exist_ok=True)
    # osacompile refuses to overwrite, and a stale bundle would keep the old
    # command line, so always start from nothing.
    shutil.rmtree(app, ignore_errors=True)

    source = _applescript_source(command)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".applescript", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(source)
        script_path = handle.name
    try:
        result = subprocess.run(
            ["/usr/bin/osacompile", "-o", str(app), script_path],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"osacompile failed: {result.stderr.strip()}")
    finally:
        os.unlink(script_path)

    _claim_scheme_in_plist(app / "Contents" / "Info.plist")

    # Nudge Launch Services so the scheme resolves without a logout.
    if os.path.exists(_LSREGISTER):
        subprocess.run([_LSREGISTER, "-f", str(app)], check=False)
    return str(app)


def _applescript_quote(value: str) -> str:
    """Render a Python string as a single AppleScript string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _applescript_source(command: str) -> str:
    """The applet's handler. `command` must already be one quoted literal.

    `with timeout` because the operator has to find the window, solve a
    captcha and sign in — well past AppleScript's default patience.
    """
    return (
        "on open location this_URL\n"
        "    with timeout of 3600 seconds\n"
        f"        do shell script {command} & \" \" & quoted form of this_URL\n"
        "    end timeout\n"
        "end open location\n"
    )


def _claim_scheme_in_plist(plist_path: Path) -> None:
    """Add our URL scheme to the applet's generated Info.plist."""
    info = plistlib.loads(plist_path.read_bytes())
    info["CFBundleName"] = "AG Connect"
    info["CFBundleIdentifier"] = "com.accessgrid.agconnect"
    # Background-only: the window the operator sees belongs to Chromium, and
    # a Dock icon for a handler that does nothing visible is just confusing.
    info["LSUIElement"] = True
    info["CFBundleURLTypes"] = [
        {"CFBundleURLName": "AG Connect", "CFBundleURLSchemes": [URI_SCHEME]}
    ]
    plist_path.write_bytes(plistlib.dumps(info))


def _unregister_macos() -> str:
    app = _mac_app_dir()
    shutil.rmtree(app, ignore_errors=True)
    if os.path.exists(_LSREGISTER):
        subprocess.run([_LSREGISTER, "-u", str(app)], check=False)
    return f"Removed {app}"


# -- public ------------------------------------------------------------------


def register() -> str:
    if sys.platform == "win32":
        return _register_windows()
    if sys.platform == "darwin":
        return _register_macos()
    raise RuntimeError(
        f"{URI_SCHEME}:// registration is not supported on {sys.platform}. "
        f"Run 'agsync connect <{URI_SCHEME}://...>' by hand instead."
    )


def unregister() -> str:
    if sys.platform == "win32":
        return _unregister_windows()
    if sys.platform == "darwin":
        return _unregister_macos()
    raise RuntimeError(f"Nothing to remove on {sys.platform}")


def is_registered() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                rf"Software\Classes\{URI_SCHEME}\shell\open\command",
            ):
                return True
        except FileNotFoundError:
            return False
    if sys.platform == "darwin":
        return (_mac_app_dir() / "Contents" / "Info.plist").exists()
    return False

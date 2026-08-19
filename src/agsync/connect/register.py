"""Register (and remove) the ``agconnect://`` URI scheme.

A web page cannot start a local process, and the service cannot put a window
on the operator's desktop, so the OS is the bridge: the wizard renders an
``agconnect://`` link and the OS hands it to this executable. Registration is
per-user — no elevation, and nothing machine-wide to clean up.

Windows writes the standard ``HKCU\\Software\\Classes`` protocol handler.
macOS has no equivalent for a bare executable, so we generate a minimal
application bundle in ~/Applications whose Info.plist claims the scheme and
whose entry point execs this same binary; Launch Services picks it up.
"""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .protocol import URI_SCHEME

logger = logging.getLogger(__name__)

_MAC_APP_NAME = "AG Connect.app"


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
    app = _mac_app_dir()
    macos_dir = app / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)

    info = {
        "CFBundleName": "AG Connect",
        "CFBundleIdentifier": "com.accessgrid.agconnect",
        "CFBundleExecutable": "agconnect",
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        # Background-only: the window the operator sees belongs to Chromium.
        "LSUIElement": True,
        "CFBundleURLTypes": [
            {
                "CFBundleURLName": "AG Connect",
                "CFBundleURLSchemes": [URI_SCHEME],
            }
        ],
    }
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))

    quoted = " ".join([f'"{program}"', *[f'"{a}"' for a in leading]])
    shim = macos_dir / "agconnect"
    shim.write_text(f'#!/bin/sh\nexec {quoted} "$1"\n', encoding="utf-8")
    shim.chmod(0o755)

    # Nudge Launch Services so the scheme resolves without a logout.
    lsregister = (
        "/System/Library/Frameworks/CoreServices.framework/Frameworks"
        "/LaunchServices.framework/Support/lsregister"
    )
    if os.path.exists(lsregister):
        subprocess.run([lsregister, "-f", str(app)], check=False)
    return f"{app}"


def _unregister_macos() -> str:
    app = _mac_app_dir()
    shutil.rmtree(app, ignore_errors=True)
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

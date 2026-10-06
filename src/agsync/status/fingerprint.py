"""A stable identity for this hub install.

One AccessGrid account can run several hubs — and running two against the
same PACS is a misconfiguration that has already cost us a production
incident, so AccessGrid needs to tell them apart.

The id is derived rather than generated, which means there is no state to
lose: it survives a wiped database, a reinstall and an upgrade. Two inputs
are deliberately left out of the hash, because both would silently mint a
new id and orphan the install's history at AccessGrid:

  * the OS version — a Windows update would re-identify the machine. It is
    reported as a field instead, so it is still visible.
  * anything MAC-derived — `uuid.getnode()` falls back to a *random* value
    when it cannot read a NIC, so a box with no network would change
    identity on every restart.

What is left is the hostname, the platform, the architecture and the
machine id the OS itself keeps. If the machine id cannot be read the
fingerprint degrades to the other three rather than inventing anything: a
slightly weaker id that stays put beats a strong one that moves.
"""

from __future__ import annotations

import hashlib
import logging
import platform
import socket
import subprocess
import sys

logger = logging.getLogger(__name__)

# Long enough that collisions are not a concern, short enough to read out
# over the phone to support.
_ID_CHARS = 32

# Reading the machine id shells out on macOS, so it is done once per
# process. None means "not computed yet"; the empty string means "tried and
# could not".
_machine_id: str | None = None
_instance_id: str | None = None


def _windows_machine_id() -> str:
    import winreg  # noqa: PLC0415 — Windows only

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
    ) as key:
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
    return str(value)


def _macos_machine_id() -> str:
    # IOPlatformUUID is the closest thing macOS has to a durable serial that
    # is readable without privileges.
    out = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        capture_output=True, text=True, timeout=5, check=False,
    ).stdout
    for line in out.splitlines():
        if "IOPlatformUUID" in line:
            return line.split('"')[-2]
    return ""


def _linux_machine_id() -> str:
    from pathlib import Path  # noqa: PLC0415

    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def machine_id() -> str:
    """The OS's own durable id for this computer, or "" if unreadable."""
    global _machine_id
    if _machine_id is not None:
        return _machine_id

    readers = {
        "win32": _windows_machine_id,
        "darwin": _macos_machine_id,
    }
    reader = readers.get(sys.platform, _linux_machine_id)
    try:
        _machine_id = (reader() or "").strip()
    except Exception as e:  # noqa: BLE001 — identity must never raise
        logger.debug("Could not read this machine's id: %s", e)
        _machine_id = ""
    if not _machine_id:
        logger.debug(
            "No OS machine id available — the instance id will rest on the "
            "hostname alone"
        )
    return _machine_id


def hostname() -> str:
    try:
        return socket.gethostname() or "unknown"
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not read the hostname: %s", e)
        return "unknown"


def instance_id() -> str:
    """A stable 32-character id for this install.

    Not a secret and not authentication: anyone who knows the hostname can
    compute a good guess at it. It identifies, it does not authorise.
    """
    global _instance_id
    if _instance_id is not None:
        return _instance_id

    # Separated by a character that cannot appear in any part, so no two
    # different machines can produce the same joined string.
    material = "\x1f".join((
        sys.platform,
        platform.machine(),
        hostname(),
        machine_id(),
    ))
    _instance_id = hashlib.sha256(material.encode("utf-8")).hexdigest()[:_ID_CHARS]
    return _instance_id


def describe() -> dict[str, str]:
    """The `instance` block of a status report, minus the parts that move."""
    def _safe(fn, default: str = "") -> str:
        try:
            return str(fn() or default)
        except Exception:  # noqa: BLE001
            return default

    from .. import __version__

    # platform.version() is the build number on Windows ("10.0.19045") and
    # a whole kernel banner everywhere else, so take the short one there.
    version = platform.version if sys.platform == "win32" else platform.release

    return {
        "id": instance_id(),
        "hostname": hostname(),
        "agent_version": __version__,
        "platform": sys.platform,
        "os_version": _safe(version, "unknown"),
        "arch": _safe(platform.machine, "unknown"),
    }


def _reset_for_tests() -> None:
    global _machine_id, _instance_id
    _machine_id = None
    _instance_id = None

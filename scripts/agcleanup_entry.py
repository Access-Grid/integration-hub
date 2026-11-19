"""PyInstaller entry point for ag-cleanup.

Kept apart from the service entry point, as agdiag is: it must never import a
path that runs migrations or generates an encryption key. Waits for Enter at
the end so a double-clicked window does not close before it can be read.
"""
import sys
import traceback

from agsync.cleanup import main


def _pause() -> None:
    if getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to exit...")
        except (EOFError, OSError):
            pass


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        code = 1
    _pause()
    sys.exit(code)

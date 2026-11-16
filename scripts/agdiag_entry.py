"""PyInstaller entry point for agdiag — the read-only state dump.

Separate from the service entry point on purpose: this must never import a
path that opens the database for writing or generates an encryption key.
"""
import sys
import traceback

from agsync.diagnostics import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        try:
            input("\nPress Enter to exit...")
        except (EOFError, OSError):
            pass
        sys.exit(1)

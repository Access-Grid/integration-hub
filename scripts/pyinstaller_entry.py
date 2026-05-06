import sys
import traceback

from agsync.__main__ import _bootstrap_logging, cli


def _pause_if_console() -> None:
    """Keep the console open after a crash so a double-clicked exe is debuggable."""
    try:
        input("\nPress Enter to exit...")
    except (EOFError, OSError):
        pass


if __name__ == "__main__":
    _bootstrap_logging()
    try:
        cli()
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        _pause_if_console()
        sys.exit(1)

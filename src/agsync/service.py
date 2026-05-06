"""Windows Service wrapper.

Imports of pywin32 are guarded so this file is importable on macOS/Linux
(and during PyInstaller's analysis pass on non-Windows hosts), but
calling the install/start functions on a non-Windows host is an error.
"""

from __future__ import annotations

import sys

SERVICE_NAME = "AGSyncTool"
SERVICE_DISPLAY_NAME = "AccessGrid Sync"
SERVICE_DESCRIPTION = "Synchronizes credentials from a PACS into AccessGrid."


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows Service operations require Windows.")


def install_service() -> None:
    _require_windows()
    import servicemanager  # noqa: F401  # pyright: ignore[reportMissingImports]
    import win32serviceutil  # pyright: ignore[reportMissingImports]

    win32serviceutil.HandleCommandLine(
        AGSyncService,
        argv=[sys.argv[0], "install"],
    )
    win32serviceutil.HandleCommandLine(
        AGSyncService,
        argv=[sys.argv[0], "start"],
    )


def uninstall_service() -> None:
    _require_windows()
    import win32serviceutil  # pyright: ignore[reportMissingImports]

    win32serviceutil.HandleCommandLine(
        AGSyncService,
        argv=[sys.argv[0], "stop"],
    )
    win32serviceutil.HandleCommandLine(
        AGSyncService,
        argv=[sys.argv[0], "remove"],
    )


if sys.platform == "win32":  # pragma: no cover
    import threading

    import servicemanager  # pyright: ignore[reportMissingImports]
    import win32event  # pyright: ignore[reportMissingImports]
    import win32service  # pyright: ignore[reportMissingImports]
    import win32serviceutil  # pyright: ignore[reportMissingImports]

    class AGSyncService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._server_thread: threading.Thread | None = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self._stop_event)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            self._server_thread = threading.Thread(target=self._serve, daemon=True)
            self._server_thread.start()
            win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)

        def _serve(self) -> None:
            import uvicorn

            from .config import get_settings
            from .observability import init_sentry

            init_sentry()
            settings = get_settings()
            uvicorn.run(
                "agsync.server:create_app",
                factory=True,
                host=settings.host,
                port=settings.port,
                log_level=settings.log_level.lower(),
            )

else:  # non-Windows: provide a stub class so `import` doesn't blow up.
    class AGSyncService:  # type: ignore[no-redef]
        pass

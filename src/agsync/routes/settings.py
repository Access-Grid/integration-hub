from __future__ import annotations

import socket

from fastapi import APIRouter, Depends, Request

from ..auth import require_admin
from ..config import get_settings
from ..settings_store import AccessGridConfig, PacsConfig

router = APIRouter()


def _local_ips() -> list[str]:
    out: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            family, *_, sockaddr = info
            if family == socket.AF_INET:
                ip = sockaddr[0]
                if not ip.startswith("127."):
                    out.add(ip)
    except OSError:
        pass
    return sorted(out)


@router.get("/settings")
def settings_page(request: Request, _user=Depends(require_admin)):
    s = get_settings()
    ag = AccessGridConfig.load() or {}
    pacs = PacsConfig.load() or {}
    return request.app.state.template_response(
        request, "settings.html",
        {
            "ag_account": ag.get("account_id", ""),
            "ag_template": ag.get("template_id", ""),
            "pacs_vendor": pacs.get("vendor", ""),
            "pacs_params_keys": list((pacs.get("params") or {}).keys()),
            "db_path": str(s.db_path),
            "host_port": f"{s.host}:{s.port}",
            "ips": _local_ips(),
            "version": __import__("agsync").__version__,
        },
    )

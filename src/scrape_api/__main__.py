"""Entry point: `scrape-api` (used by the systemd unit)."""

from __future__ import annotations

import logging
import os

import uvicorn

from .config import Settings

SD_LISTEN_FDS_START = 3


def _systemd_socket() -> int | None:
    """The listening socket systemd passed in (socket activation), if any."""
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
        return SD_LISTEN_FDS_START
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    settings.validate()
    fd = _systemd_socket()
    listen = {"fd": fd} if fd is not None else {"host": settings.host, "port": settings.port}
    uvicorn.run(
        "scrape_api.main:create_app",
        factory=True,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level="info",
        **listen,
    )


if __name__ == "__main__":
    main()

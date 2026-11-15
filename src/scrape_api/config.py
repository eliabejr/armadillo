"""Settings read from the environment (see deploy/install.sh for the env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _list(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {value!r}") from exc


@dataclass(frozen=True)
class Settings:
    api_keys: tuple[str, ...] = ()
    proxy_urls: tuple[str, ...] = ()
    allow_private_urls: bool = False
    max_concurrency: int = 4
    page_timeout_ms: int = 30_000
    max_crawl_pages: int = 500
    crawl_ttl_seconds: int = 24 * 3600
    max_crawl_jobs: int = 50
    max_crawl_bytes: int = 256 * 1024 * 1024
    max_total_crawl_bytes: int = 1024 * 1024 * 1024
    public_url: str | None = None
    host: str = "127.0.0.1"
    port: int = 3002

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_keys=_list("SCRAPE_API_KEYS"),
            proxy_urls=_list("SCRAPE_PROXY_URLS"),
            allow_private_urls=_bool("SCRAPE_ALLOW_PRIVATE_URLS", False),
            max_concurrency=_int("SCRAPE_MAX_CONCURRENCY", 4),
            page_timeout_ms=_int("SCRAPE_PAGE_TIMEOUT_MS", 30_000),
            max_crawl_pages=_int("SCRAPE_MAX_CRAWL_PAGES", 500),
            crawl_ttl_seconds=_int("SCRAPE_CRAWL_TTL_SECONDS", 24 * 3600),
            max_crawl_jobs=_int("SCRAPE_MAX_CRAWL_JOBS", 50),
            max_crawl_bytes=_int("SCRAPE_MAX_CRAWL_MB", 256) * 1024 * 1024,
            max_total_crawl_bytes=_int("SCRAPE_MAX_TOTAL_CRAWL_MB", 1024) * 1024 * 1024,
            public_url=(os.environ.get("SCRAPE_PUBLIC_URL") or "").rstrip("/") or None,
            host=os.environ.get("SCRAPE_HOST", "127.0.0.1"),
            port=_int("SCRAPE_PORT", 3002),
        )

    def validate(self) -> None:
        if not self.api_keys:
            raise RuntimeError("No API key configured: set SCRAPE_API_KEYS")
        limits = {
            "max_concurrency": "SCRAPE_MAX_CONCURRENCY",
            "page_timeout_ms": "SCRAPE_PAGE_TIMEOUT_MS",
            "max_crawl_pages": "SCRAPE_MAX_CRAWL_PAGES",
            "max_crawl_jobs": "SCRAPE_MAX_CRAWL_JOBS",
            "max_crawl_bytes": "SCRAPE_MAX_CRAWL_MB",
            "max_total_crawl_bytes": "SCRAPE_MAX_TOTAL_CRAWL_MB",
        }
        for attr, env in limits.items():
            if getattr(self, attr) < 1:
                raise RuntimeError(f"{env} must be at least 1")

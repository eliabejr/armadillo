"""Request bodies (Firecrawl field names) and format parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SUPPORTED_FORMATS = ("markdown", "html", "rawHtml", "links", "screenshot")
_ALIASES = {"raw_html": "rawHtml", "rawhtml": "rawHtml", "screenshot@fullPage": "screenshot"}

# "enhanced" is firecrawl-py's newer name for the proxied path.
ProxyMode = Literal["basic", "stealth", "enhanced", "auto"]


class InvalidRequest(ValueError):
    """A request we understood but cannot serve; answered with 400."""


@dataclass(frozen=True)
class FormatSpec:
    names: frozenset[str]


def parse_formats(formats: list[Any] | None) -> FormatSpec:
    """Accept v1 strings, v2 strings and v2 objects; reject what we cannot produce.

    Screenshots are always full-page (Crawl4AI's behaviour), so `fullPage` is accepted and ignored.
    """
    if not formats:
        return FormatSpec(frozenset({"markdown"}))
    names: set[str] = set()
    for item in formats:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict) and isinstance(item.get("type"), str):
            name = item["type"]
        else:
            raise InvalidRequest(f"invalid format entry: {item!r}")
        name = _ALIASES.get(name, name)
        if name not in SUPPORTED_FORMATS:
            raise InvalidRequest(
                f"unsupported format {name!r}; supported: {', '.join(SUPPORTED_FORMATS)}"
            )
        names.add(name)
    return FormatSpec(frozenset(names))


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ScrapeOptionsBody(_Body):
    formats: list[Any] | None = None
    onlyMainContent: bool = True
    includeTags: list[str] = Field(default_factory=list)
    excludeTags: list[str] = Field(default_factory=list)
    waitFor: int = Field(default=0, ge=0, le=60_000)
    timeout: int | None = Field(default=None, ge=1_000, le=300_000)
    proxy: ProxyMode = "auto"


class ScrapeBody(ScrapeOptionsBody):
    url: str


class MapBody(_Body):
    url: str
    search: str | None = None
    sitemap: Literal["include", "skip", "only"] = "include"
    includeSubdomains: bool = True
    ignoreQueryParameters: bool = True
    limit: int = Field(default=5000, ge=1, le=30_000)
    timeout: int | None = Field(default=None, ge=1_000, le=300_000)
    # v1 names
    ignoreSitemap: bool | None = None
    sitemapOnly: bool | None = None


class CrawlBody(_Body):
    url: str
    limit: int = Field(default=100, ge=1)
    maxDiscoveryDepth: int | None = Field(default=None, ge=0)
    includePaths: list[str] = Field(default_factory=list)
    excludePaths: list[str] = Field(default_factory=list)
    sitemap: Literal["include", "skip", "only"] = "include"
    ignoreQueryParameters: bool = False
    crawlEntireDomain: bool = False
    allowExternalLinks: bool = False
    allowSubdomains: bool = False
    delay: float = Field(default=0, ge=0, le=60)
    maxConcurrency: int | None = Field(default=None, ge=1)
    scrapeOptions: ScrapeOptionsBody | None = None
    # v1 names
    maxDepth: int | None = Field(default=None, ge=0)
    allowBackwardLinks: bool | None = None
    ignoreSitemap: bool | None = None

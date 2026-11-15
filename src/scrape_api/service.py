"""Shared state and the entry points used by both the REST routes and the MCP tools."""

from __future__ import annotations

import re

from pydantic import ValidationError

from .config import Settings
from .crawl import DEFAULT_CRAWL_CONCURRENCY, CrawlJob, CrawlManager, CrawlRequest, CrawlScope, TooManyJobs
from .engine import Engine, FetchError, NoProxyConfigured, ScrapeParams
from .mapper import Mapper
from .models import CrawlBody, InvalidRequest, MapBody, ScrapeOptionsBody, parse_formats
from .urlguard import URLNotAllowed, check_url

# Failures caused by the request or the target site, reported to the caller as such.
USER_ERRORS = (URLNotAllowed, NoProxyConfigured, FetchError, InvalidRequest, TooManyJobs, ValidationError)

_HAS_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def with_scheme(url: str) -> str:
    url = url.strip()
    return url if _HAS_SCHEME.match(url) else f"https://{url}"


def scrape_params(body: ScrapeOptionsBody, *, check_robots: bool = False) -> ScrapeParams:
    return ScrapeParams(
        formats=parse_formats(body.formats),
        only_main_content=body.onlyMainContent,
        include_tags=body.includeTags,
        exclude_tags=body.excludeTags,
        wait_for_ms=body.waitFor,
        timeout_ms=body.timeout,
        proxy=body.proxy,
        check_robots=check_robots,
    )


class Services:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = Engine(settings)
        self.mapper = Mapper(self.engine, settings)
        self.crawls = CrawlManager(self.engine, self.mapper, settings)

    async def start(self) -> None:
        await self.engine.start()

    async def close(self) -> None:
        await self.crawls.close()
        await self.engine.close()

    async def map(self, body: MapBody) -> list[dict[str, str]]:
        sitemap = body.sitemap
        if body.sitemapOnly:
            sitemap = "only"
        elif body.ignoreSitemap:
            sitemap = "skip"
        return await self.mapper.map(
            with_scheme(body.url),
            search=body.search,
            sitemap=sitemap,
            include_subdomains=body.includeSubdomains,
            ignore_query=body.ignoreQueryParameters,
            limit=body.limit,
            timeout_ms=body.timeout,
        )

    async def start_crawl(self, body: CrawlBody) -> CrawlJob:
        url = await check_url(with_scheme(body.url), self.settings.allow_private_urls)
        params = scrape_params(body.scrapeOptions or ScrapeOptionsBody(), check_robots=True)
        if params.proxy in ("stealth", "enhanced") and not self.engine.has_proxy:
            raise NoProxyConfigured(f"proxy '{params.proxy}' requested but no proxy is configured")
        scope = CrawlScope.build(
            url,
            include_paths=body.includePaths,
            exclude_paths=body.excludePaths,
            allow_subdomains=body.allowSubdomains,
            allow_external=body.allowExternalLinks,
            entire_domain=body.crawlEntireDomain or bool(body.allowBackwardLinks),
        )
        request = CrawlRequest(
            url=url,
            limit=min(body.limit, self.settings.max_crawl_pages),
            max_depth=body.maxDiscoveryDepth if body.maxDiscoveryDepth is not None else body.maxDepth,
            scope=scope,
            sitemap="skip" if body.ignoreSitemap else body.sitemap,
            ignore_query=body.ignoreQueryParameters,
            delay=body.delay,
            concurrency=min(body.maxConcurrency or DEFAULT_CRAWL_CONCURRENCY, self.settings.max_concurrency),
            params=params,
        )
        return self.crawls.start(request)

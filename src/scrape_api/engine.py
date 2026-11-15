"""One shared Crawl4AI browser, with the direct-first proxy policy and a private-network guard."""

from __future__ import annotations

import asyncio
import itertools
import logging
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig, ProxyConfig
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from .blocking import blocked_reason, is_block_like_error
from .config import Settings
from .htmlinfo import Link, parse_page
from .models import FormatSpec, ProxyMode
from .urlguard import URLNotAllowed, check_url, is_literal_private

log = logging.getLogger(__name__)

CRAWL4AI_ANTIBOT_PREFIX = "Blocked by anti-bot protection"
CRAWL4AI_ROBOTS_MESSAGE = "Access denied by robots.txt"
BROWSER_GONE_MARKERS = ("has been closed", "Browser closed", "Connection closed", "Target closed")
MAIN_CONTENT_EXCLUDED_TAGS = ["nav", "header", "footer", "aside", "noscript"]
MAX_TEXT_CHARS = 5 * 1024 * 1024


class FetchError(Exception):
    def __init__(self, message: str, *, block_like: bool = False, robots_blocked: bool = False):
        super().__init__(message)
        self.block_like = block_like
        self.robots_blocked = robots_blocked


class NoProxyConfigured(Exception):
    pass


@dataclass
class ScrapeParams:
    formats: FormatSpec = field(default_factory=lambda: FormatSpec(frozenset({"markdown"})))
    only_main_content: bool = True
    include_tags: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)
    wait_for_ms: int = 0
    timeout_ms: int | None = None
    proxy: ProxyMode = "auto"
    check_robots: bool = False


def _data_uri(b64: str | None) -> str | None:
    if not b64:
        return None
    mime = "image/jpeg" if b64.startswith("/9j/") else "image/png"
    return f"data:{mime};base64,{b64}"


@dataclass
class Page:
    source_url: str
    final_url: str
    status_code: int | None
    raw_html: str
    cleaned_html: str
    markdown: str
    screenshot: str | None
    content_type: str | None
    proxy_used: str
    links: list[Link]
    metadata: dict[str, str] = field(default_factory=dict)
    warning: str | None = None
    scrape_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def document(self, formats: FormatSpec) -> dict[str, Any]:
        doc: dict[str, Any] = {}
        if "markdown" in formats.names:
            doc["markdown"] = self.markdown
        if "html" in formats.names:
            doc["html"] = self.cleaned_html
        if "rawHtml" in formats.names:
            doc["rawHtml"] = self.raw_html
        if "links" in formats.names:
            doc["links"] = [link.url for link in self.links]
        if "screenshot" in formats.names:
            doc["screenshot"] = _data_uri(self.screenshot)
        doc["metadata"] = {
            **self.metadata,
            "sourceURL": self.source_url,
            "url": self.final_url,
            "statusCode": self.status_code,
            "contentType": self.content_type,
            "proxyUsed": self.proxy_used,
            "scrapeId": self.scrape_id,
            "creditsUsed": 1,
        }
        if self.warning:
            doc["warning"] = self.warning
        return doc


def proxy_config_from_url(url: str) -> ProxyConfig:
    parts = urlsplit(url)
    if not parts.hostname or not parts.port:
        raise ValueError(f"proxy URL needs a host and port: {parts.scheme}://{parts.hostname}")
    scheme = parts.scheme or "http"
    return ProxyConfig(
        server=f"{scheme}://{parts.hostname}:{parts.port}",
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
    )


def _browser_gone(message: str | None) -> bool:
    return bool(message) and any(marker in message for marker in BROWSER_GONE_MARKERS)


def _browser_connected(crawler) -> bool:
    try:
        return bool(crawler.crawler_strategy.browser_manager.browser.is_connected())
    except Exception:
        return False


class Engine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._proxies = [proxy_config_from_url(u) for u in settings.proxy_urls]
        self._proxy_cycle = itertools.cycle(self._proxies) if self._proxies else None
        self._secrets = [s for p in self._proxies for s in (p.username, p.password) if s]
        # Requests the browser makes to private IP literals or localhost are aborted.
        # This is a first layer only: redirect hops and names that resolve privately
        # are stopped by the systemd unit (IPAddressDeny) when the socket connects.
        self._guard = not settings.allow_private_urls
        self._guarded_contexts: weakref.WeakSet = weakref.WeakSet()
        self._crawler: AsyncWebCrawler | None = None
        self._restart_lock = asyncio.Lock()

    @property
    def has_proxy(self) -> bool:
        return bool(self._proxies)

    async def start(self) -> None:
        self._crawler = await self._new_crawler()

    async def close(self) -> None:
        if self._crawler is not None:
            await self._crawler.close()
            self._crawler = None

    async def _new_crawler(self) -> AsyncWebCrawler:
        crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
        if self._guard:
            crawler.crawler_strategy.set_hook("on_page_context_created", self._install_guard)
        await crawler.start()
        return crawler

    async def _install_guard(self, page, context=None, **kwargs):
        # On the context, so popups are covered too. Requests this cannot see
        # (redirect hops, workers, websockets) are refused by the kernel filter.
        target = context if context is not None else page
        if target in self._guarded_contexts:
            return page
        self._guarded_contexts.add(target)

        async def guard(route, request):
            try:
                if is_literal_private(request.url):
                    log.warning("blocked browser request to a private address: %s", request.url[:200])
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()
            except Exception as exc:  # the page went away while routing
                log.debug("route guard: %s", exc)

        await target.route("**/*", guard)
        return page

    def _scrub(self, message: str) -> str:
        for secret in self._secrets:
            message = message.replace(secret, "***")
        return message

    async def scrape(self, url: str, params: ScrapeParams) -> Page:
        page = await self._scrape(await check_url(url, self.settings.allow_private_urls), params)
        page.source_url = url  # Firecrawl reports the URL as the caller sent it
        return page

    async def _scrape(self, url: str, params: ScrapeParams) -> Page:
        mode = "stealth" if params.proxy == "enhanced" else params.proxy

        if mode == "stealth":
            if not self._proxy_cycle:
                raise NoProxyConfigured("proxy 'stealth' requested but no proxy is configured")
            page = await self._attempt(url, params, next(self._proxy_cycle))
        else:
            try:
                page = await self._attempt(url, params, None)
            except FetchError as exc:
                if not (mode == "auto" and exc.block_like and self._proxy_cycle):
                    raise
                log.info("direct fetch of %s failed (%s); retrying via proxy", url, exc)
                page = await self._attempt(url, params, next(self._proxy_cycle))
            else:
                reason = blocked_reason(page.status_code, page.raw_html)
                if reason and mode == "auto" and self._proxy_cycle:
                    log.info("direct fetch of %s looks blocked (%s); retrying via proxy", url, reason)
                    try:
                        page = await self._attempt(url, params, next(self._proxy_cycle))
                    except FetchError as exc:
                        page.warning = f"looked blocked ({reason}); proxy retry failed: {exc}"
                        return page

        reason = blocked_reason(page.status_code, page.raw_html)
        if reason and not page.warning:
            hint = "; no proxy configured to retry" if mode == "auto" and not self._proxy_cycle else ""
            page.warning = f"looked blocked ({reason}){hint}"
        return page

    def _run_config(self, params: ScrapeParams, proxy: ProxyConfig | None) -> CrawlerRunConfig:
        timeout_ms = params.timeout_ms or self.settings.page_timeout_ms
        excluded = list(MAIN_CONTENT_EXCLUDED_TAGS) if params.only_main_content else None
        return CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            markdown_generator=DefaultMarkdownGenerator(),
            page_timeout=timeout_ms,
            delay_before_return_html=params.wait_for_ms / 1000,
            screenshot="screenshot" in params.formats.names,
            target_elements=params.include_tags or None,
            excluded_tags=excluded,
            excluded_selector=", ".join(params.exclude_tags) or None,
            check_robots_txt=params.check_robots,
            proxy_config=proxy,
            verbose=False,
        )

    async def _restart(self, dead: AsyncWebCrawler | None) -> None:
        async with self._restart_lock:
            if self._crawler is not dead:
                return  # another request already replaced it
            log.warning("the browser is gone; starting a new one")
            try:
                if dead is not None:
                    await dead.close()
            except Exception as exc:
                log.debug("closing the dead browser: %s", exc)
            self._crawler = await self._new_crawler()

    async def _run(self, url: str, config: CrawlerRunConfig, budget: float):
        for attempt in (1, 2):
            crawler = self._crawler
            if crawler is None:
                raise RuntimeError("engine not started")
            try:
                result = await asyncio.wait_for(crawler.arun(url, config=config), budget)
            except asyncio.TimeoutError as exc:
                raise FetchError(f"Timeout after {budget:.0f}s", block_like=True) from exc
            except Exception as exc:
                # A closed page or context says "has been closed" too: restart only
                # when the browser itself is gone, not on every such message.
                if attempt == 1 and _browser_gone(str(exc)) and not _browser_connected(crawler):
                    await self._restart(crawler)
                    continue
                raise FetchError(self._scrub(str(exc)) or type(exc).__name__) from exc
            if (
                not result.success
                and attempt == 1
                and _browser_gone(result.error_message)
                and not _browser_connected(crawler)
            ):
                await self._restart(crawler)
                continue
            return result
        raise FetchError("the browser failed twice")

    async def _attempt(self, url: str, params: ScrapeParams, proxy: ProxyConfig | None) -> Page:
        config = self._run_config(params, proxy)
        budget = (params.timeout_ms or self.settings.page_timeout_ms) / 1000 + params.wait_for_ms / 1000 + 30
        async with self._semaphore:
            result = await self._run(url, config, budget)

        if not result.success:
            message = self._scrub(result.error_message or "fetch failed")
            if message.startswith(CRAWL4AI_ANTIBOT_PREFIX):
                if not result.html:
                    raise FetchError(message, block_like=True)
                # Crawl4AI's own heuristic also flags short normal pages; the page was
                # fetched and processed, so keep it and let blocked_reason() decide.
                log.debug("ignoring Crawl4AI verdict for %s: %s", url, message)
            elif CRAWL4AI_ROBOTS_MESSAGE in message:
                raise FetchError(message, robots_blocked=True)
            else:
                raise FetchError(message, block_like=is_block_like_error(message))

        final_url = result.redirected_url or url
        if self._guard and final_url != url:
            try:
                await check_url(final_url, allow_private=False)
            except URLNotAllowed as exc:
                # A redirect or script took the browser somewhere private: drop the content.
                raise URLNotAllowed("the page redirected to a private or reserved address") from exc

        raw_html = (result.html or "")[:MAX_TEXT_CHARS]
        markdown = result.markdown
        markdown_text = getattr(markdown, "raw_markdown", None) or (str(markdown) if markdown else "")
        links, metadata = await asyncio.to_thread(parse_page, raw_html, final_url)
        headers = {k.lower(): v for k, v in (result.response_headers or {}).items()}
        return Page(
            source_url=url,
            final_url=final_url,
            status_code=result.status_code,
            raw_html=raw_html,
            cleaned_html=(result.cleaned_html or "")[:MAX_TEXT_CHARS],
            markdown=markdown_text[:MAX_TEXT_CHARS],
            screenshot=result.screenshot or None,
            content_type=headers.get("content-type"),
            proxy_used="stealth" if proxy else "basic",
            links=links,
            metadata=metadata,
        )

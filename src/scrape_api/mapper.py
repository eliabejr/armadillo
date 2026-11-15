"""URL discovery: robots.txt sitemaps plus the links on the page itself."""

from __future__ import annotations

import asyncio
import logging
import threading
import zlib
from collections import deque
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx
from lxml import etree

from .config import Settings
from .engine import Engine, FetchError, ScrapeParams
from .htmlinfo import normalize_url
from .models import FormatSpec
from .urlguard import check_url

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; scrape-api; +https://github.com/unclecode/crawl4ai)"
MAX_SITEMAPS = 25
MAX_SITEMAP_BYTES = 20 * 1024 * 1024
MAX_SEARCH_CANDIDATES = 30_000
DEFAULT_DISCOVERY_SECONDS = 60


def bare_host(host: str) -> str:
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def host_in_scope(host: str | None, base_host: str, include_subdomains: bool) -> bool:
    if not host:
        return False
    h, b = bare_host(host), bare_host(base_host)
    return h == b or (include_subdomains and h.endswith("." + b))


def _hostname(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _xml_parser() -> etree.XMLParser:
    # No entity expansion, no network access: sitemaps are untrusted input.
    return etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)


def parse_sitemap(content: bytes) -> tuple[list[str], list[str]]:
    """Return (page URLs, child sitemap URLs) from a urlset or sitemapindex document."""
    if content[:2] == b"\x1f\x8b":
        # Bounded inflate: a gzip bomb stops at MAX_SITEMAP_BYTES.
        try:
            content = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(content, MAX_SITEMAP_BYTES)
        except zlib.error:
            return [], []
    try:
        root = etree.fromstring(content, parser=_xml_parser())
    except etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []
    tag = etree.QName(root).localname if isinstance(root.tag, str) else ""
    locs = [el.text.strip() for el in root.iterfind(".//{*}loc") if el.text and el.text.strip()]
    if tag == "sitemapindex":
        return [], locs
    return locs, []


def _accepted(
    content: bytes, accept: Callable[[str], bool], room: int, stop: threading.Event
) -> tuple[list[str], list[str]]:
    """Parse a sitemap and keep up to `room` normalized page URLs that `accept` keeps.

    Runs in a thread, which a deadline cannot cancel: it checks `stop` instead.
    """
    pages, children = parse_sitemap(content)
    kept: list[str] = []
    for page in pages:
        if stop.is_set():
            break
        normalized = normalize_url(page)
        if normalized and accept(normalized):
            kept.append(normalized)
            if len(kept) >= room:
                break
    return kept, children


def robots_sitemaps(robots_txt: str) -> list[str]:
    found = []
    for line in robots_txt.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "sitemap" and value.strip():
            found.append(value.strip())
    return found


class Mapper:
    def __init__(self, engine: Engine, settings: Settings):
        self.engine = engine
        self.settings = settings

    def _client(self) -> httpx.AsyncClient:
        allow_private = self.settings.allow_private_urls

        async def guard(request: httpx.Request) -> None:
            # Runs for every request, including each redirect hop.
            await check_url(str(request.url), allow_private)

        return httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            event_hooks={"request": [guard]},
        )

    async def _get(self, client: httpx.AsyncClient, url: str) -> bytes | None:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_SITEMAP_BYTES:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            log.info("sitemap fetch %s failed: %s", url[:200], exc)
            return None

    async def sitemap_urls(
        self,
        start_url: str,
        *,
        accept: Callable[[str], bool],
        max_urls: int,
        deadline_s: float = DEFAULT_DISCOVERY_SECONDS,
    ) -> list[str]:
        """Page URLs from the site's sitemaps that `accept` keeps, up to max_urls.

        Bounded by deadline_s overall, even against a server that trickles bytes;
        what was found by then is returned.
        """
        urls: list[str] = []
        stop = threading.Event()
        try:
            async with asyncio.timeout(deadline_s):
                await self._discover(start_url, accept, max_urls, urls, stop)
        except TimeoutError:
            log.info("sitemap discovery for %s hit its %.0fs deadline", start_url, deadline_s)
        finally:
            stop.set()
        return urls[:max_urls]

    async def _discover(
        self, start_url: str, accept: Callable[[str], bool], max_urls: int, urls: list[str], stop: threading.Event
    ) -> None:
        parts = urlsplit(start_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        base_host = parts.hostname or ""
        async with self._client() as client:
            robots = await self._get(client, origin + "/robots.txt")
            listed = robots_sitemaps(robots.decode("utf-8", "replace")) if robots else []
            queue = deque(listed or [origin + "/sitemap.xml"])
            seen: set[str] = set()
            while queue and len(seen) < MAX_SITEMAPS and len(urls) < max_urls:
                try:
                    sitemap = urljoin(origin, queue.popleft())
                except ValueError:
                    continue
                if sitemap in seen or not host_in_scope(_hostname(sitemap), base_host, True):
                    continue
                seen.add(sitemap)
                content = await self._get(client, sitemap)
                if not content:
                    continue
                pages, children = await asyncio.to_thread(_accepted, content, accept, max_urls - len(urls), stop)
                queue.extend(children)
                urls.extend(pages)

    async def map(
        self,
        url: str,
        *,
        search: str | None,
        sitemap: str,
        include_subdomains: bool,
        ignore_query: bool,
        limit: int,
        timeout_ms: int | None,
    ) -> list[dict[str, str]]:
        start = await check_url(url, self.settings.allow_private_urls)
        base_host = urlsplit(start).hostname or ""
        needle = search.lower() if search else None
        found: dict[str, str | None] = {}

        def in_site(candidate: str) -> bool:
            return host_in_scope(_hostname(candidate), base_host, include_subdomains)

        def add(candidate: str, title: str | None = None) -> None:
            key = normalize_url(candidate, drop_query=ignore_query)
            if key is None or not in_site(key):
                return
            if key not in found or (title and not found[key]):
                found[key] = title or found.get(key)

        add(start)
        if sitemap != "skip":
            # With a search, look through many more sitemap URLs than we return.
            accept = (lambda u: in_site(u) and needle in u.lower()) if needle else in_site
            budget = MAX_SEARCH_CANDIDATES if needle else limit * 2
            deadline = (timeout_ms or DEFAULT_DISCOVERY_SECONDS * 1000) / 1000
            for page in await self.sitemap_urls(start, accept=accept, max_urls=budget, deadline_s=deadline):
                add(page)
        if sitemap != "only":
            try:
                page = await self.engine.scrape(
                    start,
                    ScrapeParams(formats=FormatSpec(frozenset({"links"})), only_main_content=False, timeout_ms=timeout_ms),
                )
                add(page.final_url)
                for link in page.links:
                    add(link.url, link.text or None)
            except FetchError as exc:
                log.info("map: page fetch of %s failed: %s", start, exc)

        items = list(found.items())
        if needle:
            items = [(u, t) for u, t in items if needle in u.lower() or (t and needle in t.lower())]
        result = []
        for link, title in items[:limit]:
            entry: dict[str, str] = {"url": link}
            if title:
                entry["title"] = title
            result.append(entry)
        return result

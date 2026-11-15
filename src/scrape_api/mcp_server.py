"""Remote MCP server (streamable HTTP at /mcp) for Claude agents."""

from __future__ import annotations

import base64
from importlib.metadata import version
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from .models import CrawlBody, MapBody, ScrapeOptionsBody
from .service import USER_ERRORS, Services, scrape_params, with_scheme

INSTRUCTIONS = """\
Web scraping for agents. scrape returns one page as markdown (main content by default).
map lists the URLs of a site. crawl_start begins a crawl of many pages and returns an id;
poll crawl_status with that id. proxy="auto" (default) only uses an IP proxy when the
direct request is blocked. Respect robots.txt and do not hammer sites."""


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _truncate(text: str, max_chars: int) -> str:
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[truncated: {len(text) - max_chars} more characters]"
    return text


def _header(doc_meta: dict) -> str:
    lines = [f"URL: {doc_meta.get('url')}"]
    if doc_meta.get("title"):
        lines.append(f"Title: {doc_meta['title']}")
    lines.append(f"Status: {doc_meta.get('statusCode')} | proxy: {doc_meta.get('proxyUsed')}")
    return "\n".join(lines)


def build_mcp(services: Services) -> MCPServer:
    mcp = MCPServer("scrape-api", instructions=INSTRUCTIONS, version=version("scrape-api"))

    async def _scrape(url: str, body: ScrapeOptionsBody):
        try:
            params = scrape_params(body)
            page = await services.engine.scrape(with_scheme(url), params)
        except USER_ERRORS as exc:
            raise ToolError(str(exc)) from exc
        return page, params

    @mcp.tool()
    async def scrape(
        url: str,
        format: Literal["markdown", "html", "rawHtml", "links"] = "markdown",
        only_main_content: bool = True,
        wait_for_ms: int = 0,
        proxy: Literal["auto", "basic", "stealth"] = "auto",
        max_chars: int = 40_000,
    ) -> str:
        """Fetch one web page with a real browser and return it as markdown (or html, rawHtml, links).

        only_main_content drops nav/header/footer. wait_for_ms waits for late JavaScript.
        proxy: auto = direct, retry through an IP proxy only if blocked; basic = never; stealth = always.
        """
        page, params = await _scrape(
            url,
            ScrapeOptionsBody(
                formats=[format], onlyMainContent=only_main_content, waitFor=_clamp(wait_for_ms, 0, 60_000), proxy=proxy
            ),
        )
        doc = page.document(params.formats)
        if format == "links":
            content = "\n".join(doc["links"])
        else:
            content = doc[format]
        text = f"{_header(doc['metadata'])}\n"
        if doc.get("warning"):
            text += f"Warning: {doc['warning']}\n"
        return text + "---\n" + _truncate(content, _clamp(max_chars, 1, 200_000))

    @mcp.tool()
    async def screenshot(
        url: str,
        wait_for_ms: int = 0,
        proxy: Literal["auto", "basic", "stealth"] = "auto",
    ) -> Image:
        """Take a screenshot of a web page and return the image."""
        page, _ = await _scrape(
            url, ScrapeOptionsBody(formats=["screenshot"], waitFor=_clamp(wait_for_ms, 0, 60_000), proxy=proxy)
        )
        if not page.screenshot:
            raise ToolError("the browser returned no screenshot")
        data = base64.b64decode(page.screenshot)
        return Image(data=data, format="jpeg" if data[:3] == b"\xff\xd8\xff" else "png")

    @mcp.tool()
    async def map(
        url: str,
        search: str | None = None,
        limit: int = 200,
        include_subdomains: bool = True,
        sitemap: Literal["include", "skip", "only"] = "include",
    ) -> str:
        """List the URLs of a website (from its sitemaps and the page's links), one per line.

        search keeps only URLs (or link texts) containing that text.
        """
        try:
            links = await services.map(
                MapBody(
                    url=url,
                    search=search,
                    sitemap=sitemap,
                    includeSubdomains=include_subdomains,
                    limit=_clamp(limit, 1, 5000),
                )
            )
        except USER_ERRORS as exc:
            raise ToolError(str(exc)) from exc
        lines = [f"{item['url']}  {item['title']}" if item.get("title") else item["url"] for item in links]
        return f"{len(lines)} URLs\n" + "\n".join(lines)

    @mcp.tool()
    async def crawl_start(
        url: str,
        limit: int = 20,
        max_depth: int = 2,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
        crawl_entire_domain: bool = False,
        only_main_content: bool = True,
    ) -> str:
        """Start crawling a site from url (child paths only unless crawl_entire_domain). Returns a crawl id.

        include_paths/exclude_paths are regexes on the URL path, e.g. "^/blog/".
        Then call crawl_status with the id until status is completed.
        """
        try:
            job = await services.start_crawl(
                CrawlBody(
                    url=url,
                    limit=_clamp(limit, 1, 500),
                    maxDiscoveryDepth=_clamp(max_depth, 0, 20),
                    includePaths=include_paths or [],
                    excludePaths=exclude_paths or [],
                    crawlEntireDomain=crawl_entire_domain,
                    scrapeOptions=ScrapeOptionsBody(onlyMainContent=only_main_content),
                )
            )
        except USER_ERRORS as exc:
            raise ToolError(str(exc)) from exc
        return f"crawl id: {job.id}\nlimit: {job.limit} pages\nCall crawl_status(id=\"{job.id}\") to get progress and pages."

    @mcp.tool()
    async def crawl_status(id: str, offset: int = 0, max_pages: int = 10, max_chars_per_page: int = 4000) -> str:
        """Progress of a crawl and its pages as markdown, from offset (use the next offset it reports)."""
        job = services.crawls.get(id)
        if job is None:
            raise ToolError(f"no crawl with id {id!r} (unknown or expired)")
        offset = max(0, offset)
        max_chars_per_page = _clamp(max_chars_per_page, 1, 50_000)
        pages = job.data[offset : offset + _clamp(max_pages, 1, 50)]
        out = [
            f"status: {job.status} | completed {job.completed}/{job.total} | pages so far: {len(job.data)}"
            f" | errors: {len(job.errors)} | robots-blocked: {len(job.robots_blocked)}"
        ]
        if job.warning:
            out.append(f"warning: {job.warning}")
        if job.error:
            out.append(f"error: {job.error}")
        for i, doc in enumerate(pages, start=offset):
            meta = doc.get("metadata", {})
            out.append(f"\n## [{i}] {meta.get('title') or ''}\n{meta.get('url')}\n")
            out.append(_truncate(doc.get("markdown") or "", max_chars_per_page))
        next_offset = offset + len(pages)
        if next_offset < len(job.data):
            out.append(f"\nMore pages: call crawl_status(id=\"{id}\", offset={next_offset}).")
        elif not job.finished:
            out.append("\nStill crawling: call crawl_status again in a few seconds.")
        return "\n".join(out)

    return mcp


def mcp_http_app(mcp: MCPServer):
    # Stateless JSON responses suit a proxied public URL. A bearer key is checked
    # in front of this app, and the public host varies, so Host validation is off.
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

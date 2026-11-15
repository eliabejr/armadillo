"""AC-05 with the official firecrawl-py SDK; AC-07 MCP over streamable HTTP."""

from __future__ import annotations

import asyncio
import time

import pytest
from firecrawl import Firecrawl
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .conftest import API_KEY

case = pytest.mark.case


# --- firecrawl-py -----------------------------------------------------------------


@case("SDK-01")
def test_firecrawl_sdk_scrape_and_map(api, site):
    fc = Firecrawl(api_key=API_KEY, api_url=api.base)
    doc = fc.scrape(site.url("/"), formats=["markdown", "links"])
    assert "Main content paragraph." in doc.markdown
    assert doc.metadata.title == "Home Test"
    assert site.url("/a") in doc.links
    mapped = fc.map(site.url("/"), limit=50)
    assert site.url("/b") in {link.url for link in mapped.links}


@case("SDK-02")
def test_firecrawl_sdk_crawl_waits_and_paginates(api, site):
    fc = Firecrawl(api_key=API_KEY, api_url=api.base)
    job = fc.crawl(site.url("/many/"), limit=55, sitemap="skip", poll_interval=1, timeout=240)
    assert job.status == "completed"
    assert len(job.data) == 55
    assert all(doc.markdown for doc in job.data)


# --- MCP ------------------------------------------------------------------------------


def run_mcp(api, fn):
    async def main():
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {API_KEY}"})
        async with http, streamable_http_client(api.base + "/mcp", http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    return asyncio.run(main())


def text_of(result) -> str:
    assert not result.is_error, result.content
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


@case("MCP-01")
def test_mcp_lists_tools_and_scrapes(api, site):
    async def fn(session):
        tools = {t.name for t in (await session.list_tools()).tools}
        scraped = text_of(await session.call_tool("scrape", {"url": site.url("/")}))
        mapped = text_of(await session.call_tool("map", {"url": site.url("/")}))
        shot = await session.call_tool("screenshot", {"url": site.url("/")})
        return tools, scraped, mapped, shot

    tools, scraped, mapped, shot = run_mcp(api, fn)
    assert tools == {"scrape", "screenshot", "map", "crawl_start", "crawl_status"}
    assert "Main content paragraph." in scraped and "Title: Home Test" in scraped
    assert site.url("/blog/1") in mapped
    assert not shot.is_error and shot.content[0].type == "image" and len(shot.content[0].data) > 100


@case("MCP-02")
def test_mcp_crawl_start_and_status(api, site):
    async def fn(session):
        started = text_of(await session.call_tool("crawl_start", {"url": site.url("/blog/"), "limit": 5}))
        crawl_id = started.split("crawl id: ", 1)[1].split()[0]
        deadline = time.time() + 120
        while time.time() < deadline:
            status = text_of(await session.call_tool("crawl_status", {"id": crawl_id}))
            if status.startswith("status: completed"):
                return status
            await asyncio.sleep(1)
        raise AssertionError(status)

    status = run_mcp(api, fn)
    assert "Blog post one" in status and "Blog post two" in status


@case("MCP-03")
def test_mcp_tool_errors_are_reported(api_guarded):
    async def fn(session):
        return await session.call_tool("scrape", {"url": "http://127.0.0.1:5901/"})

    result = run_mcp(api_guarded, fn)
    assert result.is_error
    assert "private" in result.content[0].text

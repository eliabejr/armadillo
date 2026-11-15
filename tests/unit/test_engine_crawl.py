"""Engine and crawl-manager logic with fake browsers (no Chromium needed)."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from scrape_api.config import Settings
from scrape_api.crawl import PAGE_BYTES, CrawlJob, CrawlManager, CrawlRequest, CrawlScope, TooManyJobs, _doc_bytes
from scrape_api.engine import Engine, FetchError, Page, ScrapeParams
from scrape_api.htmlinfo import Link
from scrape_api.mapper import Mapper
from scrape_api.urlguard import URLNotAllowed

PROXY = "http://user1:s3cret@proxy.example:8080"


def result(html="<html><title>T</title><p>hello</p></html>", status=200, success=True, error=None, final=None):
    return SimpleNamespace(
        success=success,
        html=html,
        cleaned_html=html,
        markdown=SimpleNamespace(raw_markdown="hello"),
        status_code=status,
        redirected_url=final,
        response_headers={"Content-Type": "text/html"},
        screenshot=None,
        error_message=error,
    )


class FakeCrawler:
    """arun() answers from a function of (url, proxied)."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[tuple[str, bool]] = []
        self.closed = False

    async def arun(self, url, config):
        proxied = config.proxy_config is not None
        self.calls.append((url, proxied))
        value = self.answer(url, proxied)
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self):
        self.closed = True


def engine_with(answer, **settings) -> tuple[Engine, FakeCrawler]:
    settings.setdefault("allow_private_urls", True)
    engine = Engine(Settings(api_keys=("k",), **settings))
    fake = FakeCrawler(answer)
    engine._crawler = fake
    return engine, fake


def scrape(engine, url="http://1.1.1.1/", **params):
    return asyncio.run(engine.scrape(url, ScrapeParams(**params)))


# --- engine --------------------------------------------------------------------


def test_crawl4ai_antibot_verdict_without_html_retries_through_proxy():
    def answer(url, proxied):
        if proxied:
            return result()
        return result(html="", success=False, error="Blocked by anti-bot protection: HTTP 403")

    engine, fake = engine_with(answer, proxy_urls=(PROXY,))
    page = scrape(engine)
    assert page.proxy_used == "stealth"
    assert [p for _, p in fake.calls] == [False, True]


def test_crawl4ai_antibot_verdict_with_html_keeps_the_page():
    engine, fake = engine_with(lambda u, p: result(success=False, error="Blocked by anti-bot protection: minimal_text"))
    page = scrape(engine)
    assert page.proxy_used == "basic" and page.markdown == "hello"
    assert len(fake.calls) == 1


def test_blocked_after_proxy_retry_still_warns():
    engine, _ = engine_with(lambda u, p: result(status=403), proxy_urls=(PROXY,))
    page = scrape(engine)
    assert page.proxy_used == "stealth"
    assert "looked blocked (HTTP 403)" in page.warning


def test_enhanced_is_an_alias_for_stealth():
    engine, fake = engine_with(lambda u, p: result(), proxy_urls=(PROXY,))
    assert scrape(engine, proxy="enhanced").proxy_used == "stealth"
    assert fake.calls == [("http://1.1.1.1/", True)]


def test_proxy_credentials_are_scrubbed_from_errors():
    engine, _ = engine_with(
        lambda u, p: result(success=False, html="", error=f"net::ERR_FAILED via {PROXY}"), proxy_urls=(PROXY,)
    )
    with pytest.raises(FetchError) as info:
        scrape(engine, proxy="stealth")
    assert "s3cret" not in str(info.value) and "user1" not in str(info.value)


def test_redirect_to_private_address_drops_the_content():
    engine, _ = engine_with(lambda u, p: result(final="http://127.0.0.1:5901/"), allow_private_urls=False)
    with pytest.raises(URLNotAllowed):
        scrape(engine, url="http://1.1.1.1/")


def test_dead_browser_is_replaced_and_the_request_retried():
    engine, dead = engine_with(lambda u, p: Exception("Target page, context or browser has been closed"))
    fresh = FakeCrawler(lambda u, p: result())

    async def new_crawler():
        return fresh

    engine._new_crawler = new_crawler
    page = scrape(engine)
    assert page.markdown == "hello"
    assert dead.closed and engine._crawler is fresh


def test_closed_page_does_not_restart_a_live_browser():
    engine, crawler = engine_with(lambda u, p: Exception("Target page, context or browser has been closed"))
    crawler.crawler_strategy = SimpleNamespace(
        browser_manager=SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: True))
    )
    restarted = []

    async def new_crawler():
        restarted.append(True)
        return FakeCrawler(lambda u, p: result())

    engine._new_crawler = new_crawler
    with pytest.raises(FetchError):
        scrape(engine)
    assert not restarted and engine._crawler is crawler


def test_source_url_is_what_the_caller_sent():
    engine, fake = engine_with(lambda u, p: result())
    page = scrape(engine, url="HTTP://1.1.1.1")
    assert page.source_url == "HTTP://1.1.1.1"
    assert fake.calls[0][0] == "http://1.1.1.1/"


# --- crawl manager ---------------------------------------------------------------


class FakeEngine:
    """Serves a tiny site: {url: (final_url, [links])}."""

    def __init__(self, site, gate: asyncio.Event | None = None):
        self.site = site
        self.gate = gate
        self.fetched: list[str] = []

    async def scrape(self, url, params):
        if self.gate:
            await self.gate.wait()
        self.fetched.append(url)
        final, links = self.site.get(url, (url, []))
        return Page(
            source_url=url, final_url=final, status_code=200, raw_html="", cleaned_html="",
            markdown="x" * 100, screenshot=None, content_type=None, proxy_used="basic",
            links=[Link(u, "") for u in links],
        )


class NoSitemaps:
    async def sitemap_urls(self, *args, **kwargs):
        return []


def run_crawl(site, start, *, settings=None, ignore_query=False, limit=50):
    async def main():
        engine = FakeEngine(site)
        manager = CrawlManager(engine, NoSitemaps(), settings or Settings(api_keys=("k",)))
        scope = CrawlScope.build(start, include_paths=[], exclude_paths=[])
        job = manager.start(
            CrawlRequest(url=start, limit=limit, max_depth=None, scope=scope, sitemap="skip",
                         ignore_query=ignore_query, delay=0, concurrency=2, params=ScrapeParams())
        )
        await job.task
        return job, engine, manager

    return asyncio.run(main())


def test_ignore_query_fetches_full_url_and_dedupes_on_path():
    site = {"https://s.com/": ("https://s.com/", ["https://s.com/list?page=2", "https://s.com/list?page=3"])}
    job, engine, _ = run_crawl(site, "https://s.com/", ignore_query=True)
    assert engine.fetched == ["https://s.com/", "https://s.com/list?page=2"]
    assert job.status == "completed"


def test_scope_follows_a_redirect_of_the_start_page():
    site = {
        "https://s.com/help": ("https://help.s.com/", ["https://help.s.com/a", "https://other.com/x"]),
    }
    job, engine, _ = run_crawl(site, "https://s.com/help")
    assert "https://help.s.com/a" in engine.fetched
    assert "https://other.com/x" not in engine.fetched


def test_byte_budget_stops_the_crawl_with_a_warning():
    links = [f"https://s.com/p{i}" for i in range(20)]
    site = {"https://s.com/": ("https://s.com/", links)}
    settings = Settings(api_keys=("k",), max_crawl_bytes=3 * 1200)
    job, engine, _ = run_crawl(site, "https://s.com/", settings=settings)
    assert job.status == "completed"
    assert "stopped early" in job.warning
    assert len(job.data) < 21


def test_ttl_counts_from_the_end_of_the_crawl():
    settings = Settings(api_keys=("k",), crawl_ttl_seconds=1)
    job, _, manager = run_crawl({}, "https://s.com/", settings=settings)
    assert job.finished_at is not None and job.expires_at == pytest.approx(job.finished_at + 1)
    assert manager.get(job.id) is job
    time.sleep(1.1)
    assert manager.get(job.id) is None


def test_too_many_running_jobs():
    async def main():
        gate = asyncio.Event()
        manager = CrawlManager(FakeEngine({}, gate), NoSitemaps(), Settings(api_keys=("k",), max_crawl_jobs=1))

        def request():
            scope = CrawlScope.build("https://s.com/", include_paths=[], exclude_paths=[])
            return CrawlRequest(url="https://s.com/", limit=5, max_depth=None, scope=scope, sitemap="skip",
                                ignore_query=False, delay=0, concurrency=1, params=ScrapeParams())

        first = manager.start(request())
        with pytest.raises(TooManyJobs):
            manager.start(request())
        gate.set()
        await first.task
        manager.start(request())  # a finished job makes room
        await manager.close()

    asyncio.run(main())


def test_status_pages_are_bounded_by_bytes():
    job = CrawlJob(id="j", url="u", limit=10, created_at=time.time(), ttl=60)
    for _ in range(3):
        job.add({"markdown": "x" * (PAGE_BYTES // 2 + 1)})
    first = job.status_body(0, "https://api/v2/crawl/j")
    assert len(first["data"]) == 1 and first["next"].endswith("?skip=1")
    huge = CrawlJob(id="h", url="u", limit=10, created_at=time.time(), ttl=60)
    huge.add({"markdown": "x" * (PAGE_BYTES * 2)})
    assert len(huge.status_body(0, "b")["data"]) == 1  # one oversized doc still goes out


def test_slow_path_pattern_fails_closed():
    scope = CrawlScope.build("https://s.com/", include_paths=[r"(a|aa)+$"], exclude_paths=[])
    started = time.perf_counter()
    assert not scope.allows("https://s.com/" + "a" * 40 + "!")
    assert not scope.allows("https://s.com/aaaa")  # broken scope follows nothing more
    assert scope.broken and time.perf_counter() - started < 1


def test_memory_accounting_counts_nested_fields_and_non_ascii():
    assert _doc_bytes({"links": ["https://x/" + "a" * 1000] * 10}) > 10_000
    assert _doc_bytes({"markdown": "é" * 100}) == 64 + len("markdown") + 400


def test_global_memory_cap_stops_crawls_and_refuses_new_ones():
    links = [f"https://s.com/p{i}" for i in range(20)]
    site = {"https://s.com/": ("https://s.com/", links)}
    settings = Settings(api_keys=("k",), max_total_crawl_bytes=2500)
    job, _, manager = run_crawl(site, "https://s.com/", settings=settings)
    assert "all crawl results together" in job.warning
    assert manager.total_bytes() >= 2500

    async def again():
        scope = CrawlScope.build("https://s.com/", include_paths=[], exclude_paths=[])
        req = CrawlRequest(url="https://s.com/", limit=5, max_depth=None, scope=scope, sitemap="skip",
                           ignore_query=False, delay=0, concurrency=1, params=ScrapeParams())
        manager.start(req)  # the finished job is evicted to make room
        assert job.id not in manager.jobs
        await manager.close()

    asyncio.run(again())


def test_scope_rebase_never_mixes_two_bases():
    # Under (s.com, /help) and under (help.s.com, /) this URL is out of scope; only a
    # mix of the two (s.com, /) would let it in.
    scope = CrawlScope.build("https://s.com/help", include_paths=[], exclude_paths=[])
    stray = "https://s.com/other"
    done = threading.Event()
    leaked = []

    def reader():
        while not done.is_set():
            if scope.allows(stray):
                leaked.append(True)
                return

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for _ in range(20_000):
            scope.rebase("https://help.s.com/")
            scope.rebase("https://s.com/help")
            if leaked:
                break
    finally:
        done.set()
        thread.join()
        sys.setswitchinterval(interval)
    assert not leaked


def test_sitemap_deadline_stops_the_filter_thread():
    urlset = b"<urlset>" + b"".join(b"<url><loc>https://s.com/%d</loc></url>" % i for i in range(5000)) + b"</urlset>"
    mapper = Mapper(None, Settings(api_keys=("k",), allow_private_urls=True))
    accepted = []

    async def fake_get(client, url):
        return urlset if url.endswith("/sitemap.xml") else None

    def slow_accept(url):
        accepted.append(url)
        time.sleep(0.005)  # 5000 URLs: 25 s if the thread ignored the deadline
        return True

    mapper._get = fake_get
    started = time.monotonic()
    # asyncio.run waits for the worker thread when it closes the loop.
    urls = asyncio.run(mapper.sitemap_urls("https://s.com/", accept=slow_accept, max_urls=10_000, deadline_s=0.3))
    assert time.monotonic() - started < 3
    assert urls == [] and 0 < len(accepted) < 5000

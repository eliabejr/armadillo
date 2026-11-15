"""AC-04 map, AC-05 crawl, AC-06 v1 map/crawl."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from .conftest import MANY, lan_ip, wait_crawl

case = pytest.mark.case


def paths(urls):
    return {urlsplit(u).path for u in urls}


# --- AC-04 map ---------------------------------------------------------------------


@case("MAP-01")
def test_map_combines_sitemap_and_page_links(api, site):
    body = api.client.post("/v2/map", json={"url": site.url("/")}).json()
    assert body["success"] is True
    links = body["links"]
    found = paths(item["url"] for item in links)
    assert {"/", "/a", "/b", "/blog/", "/blog/1", "/sitemap-only-page", "/private/secret"} <= found
    assert all(urlsplit(item["url"]).hostname == site.ip for item in links)
    titles = {urlsplit(item["url"]).path: item.get("title") for item in links}
    assert titles["/b"] == "B link"


@case("MAP-02")
def test_map_search_limit_and_sitemap_modes(api, site):
    blog = api.client.post("/v2/map", json={"url": site.url("/"), "search": "blog"}).json()["links"]
    assert blog and all("blog" in item["url"].lower() or "blog" in (item.get("title") or "").lower() for item in blog)
    assert len(api.client.post("/v2/map", json={"url": site.url("/"), "limit": 2}).json()["links"]) == 2
    only = paths(i["url"] for i in api.client.post("/v2/map", json={"url": site.url("/"), "sitemap": "only"}).json()["links"])
    assert "/sitemap-only-page" in only and "/blog/" not in only
    skip = paths(i["url"] for i in api.client.post("/v2/map", json={"url": site.url("/"), "sitemap": "skip"}).json()["links"])
    assert "/sitemap-only-page" not in skip and "/blog/" in skip


@case("MAP-03")
def test_map_search_looks_past_the_limit(api, site):
    links = api.client.post("/v2/map", json={"url": site.url("/"), "search": "sitemap-only", "limit": 1}).json()["links"]
    assert [urlsplit(i["url"]).path for i in links] == ["/sitemap-only-page"]


@case("V1-02")
def test_v1_map_returns_strings(api, site):
    body = api.client.post("/v1/map", json={"url": site.url("/")}).json()
    assert body["success"] is True
    assert all(isinstance(link, str) for link in body["links"])
    assert site.url("/a") in body["links"]


# --- AC-05 crawl -------------------------------------------------------------------


def start(api, **body):
    r = api.client.post("/v2/crawl", json=body)
    assert r.status_code == 200, r.text
    started = r.json()
    assert started["success"] is True and started["id"]
    assert started["url"].endswith(f"/v2/crawl/{started['id']}")
    return started


@case("CRW-01")
def test_crawl_depth_robots_and_domain(api, site):
    started = start(api, url=site.url("/"), limit=50, maxDiscoveryDepth=1, sitemap="skip", crawlEntireDomain=True)
    status = wait_crawl(api.client, started["url"])
    assert status["status"] == "completed"
    assert status["completed"] == status["total"]
    crawled = paths(doc["metadata"]["sourceURL"] for doc in status["data"])
    assert {"/", "/a", "/b", "/blog/"} <= crawled
    assert "/a/deep" not in crawled  # depth 2
    assert "/private/secret" not in crawled  # robots.txt
    assert all(urlsplit(doc["metadata"]["sourceURL"]).hostname == site.ip for doc in status["data"])
    errors = api.client.get(started["url"] + "/errors").json()
    assert any(url.endswith("/private/secret") for url in errors["robotsBlocked"])


@case("CRW-02")
def test_crawl_stays_in_child_paths(api, site):
    status = wait_crawl(api.client, start(api, url=site.url("/blog/"), limit=20, sitemap="skip")["url"])
    assert paths(d["metadata"]["sourceURL"] for d in status["data"]) == {"/blog/", "/blog/1", "/blog/2"}


@case("CRW-03")
def test_crawl_include_exclude_and_sitemap_seeds(api, site):
    status = wait_crawl(
        api.client,
        start(api, url=site.url("/"), limit=20, crawlEntireDomain=True, includePaths=["^/a"], excludePaths=["deeper"])["url"],
    )
    crawled = paths(d["metadata"]["sourceURL"] for d in status["data"])
    assert crawled == {"/", "/a", "/a/deep"}
    seeded = wait_crawl(api.client, start(api, url=site.url("/"), limit=30, maxDiscoveryDepth=0)["url"])
    assert "/sitemap-only-page" in paths(d["metadata"]["sourceURL"] for d in seeded["data"])


@case("CRW-04")
def test_crawl_limit(api, site):
    status = wait_crawl(api.client, start(api, url=site.url("/"), limit=3, crawlEntireDomain=True)["url"])
    assert status["total"] == 3
    assert len(status["data"]) <= 3


@case("CRW-05")
def test_crawl_cancel_and_409_after_finish(api, site):
    started = start(api, url=site.url("/many/"), limit=40, delay=2, sitemap="skip")
    time.sleep(1)
    r = api.client.delete(started["url"])
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert api.client.get(started["url"]).json()["status"] == "cancelled"
    assert api.client.delete(started["url"]).status_code == 409
    assert api.client.get("/v2/crawl/does-not-exist").status_code == 404


@case("CRW-06")
def test_crawl_status_paginates_with_next(api, site):
    started = start(api, url=site.url("/many/"), limit=MANY + 1, sitemap="skip", maxConcurrency=4)
    status = wait_crawl(api.client, started["url"], timeout=240)
    assert status["status"] == "completed" and status["total"] == MANY + 1
    assert len(status["data"]) == 50
    assert status["next"].endswith("?skip=50")
    rest = api.client.get(status["next"]).json()
    assert rest["next"] is None
    all_paths = paths(d["metadata"]["sourceURL"] for d in status["data"] + rest["data"])
    assert all_paths == {"/many/"} | {f"/many/{i}" for i in range(MANY)}


@case("V1-03")
def test_v1_crawl(api, site):
    r = api.client.post("/v1/crawl", json={"url": site.url("/blog/"), "limit": 10, "maxDepth": 1, "ignoreSitemap": True})
    assert r.status_code == 200
    started = r.json()
    assert "/v1/crawl/" in started["url"]
    status = wait_crawl(api.client, started["url"])
    assert status["status"] == "completed"
    assert paths(d["metadata"]["sourceURL"] for d in status["data"]) == {"/blog/", "/blog/1", "/blog/2"}


@case("CRW-07")
def test_stealth_crawl_without_proxy_is_rejected_up_front(api_noproxy, site):
    r = api_noproxy.client.post("/v2/crawl", json={"url": site.url("/"), "scrapeOptions": {"proxy": "stealth"}})
    assert r.status_code == 400
    assert "no proxy" in r.json()["error"]


@case("MAP-04")
def test_map_discovery_has_a_hard_deadline(api):
    """A server that trickles robots.txt one byte a second must not hold map past its timeout."""

    class Trickle(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/robots.txt":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                for ch in "User-agent: *\nDisallow:\n" * 10:
                    self.wfile.write(ch.encode())
                    self.wfile.flush()
                    time.sleep(1)
                return
            body = b"<html><title>T</title><a href='/x'>x</a></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("0.0.0.0", 0), Trickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://{lan_ip()}:{server.server_address[1]}/"
        started = time.time()
        r = api.client.post("/v2/map", json={"url": url, "timeout": 3000})
        assert r.status_code == 200, r.text
        assert time.time() - started < 20
        assert url + "x" in {i["url"] for i in r.json()["links"]}
    finally:
        server.shutdown()

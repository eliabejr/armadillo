"""AC-01 auth, AC-02 scrape, AC-03 proxy policy, AC-06 v1 scrape, AC-08 URL guard."""

from __future__ import annotations

import httpx
import pytest

from .conftest import API_KEY

case = pytest.mark.case


# --- AC-01 auth ----------------------------------------------------------------


@case("AUTH-01")
def test_missing_key_is_rejected(api, site):
    r = httpx.post(api.base + "/v2/scrape", json={"url": site.url("/")})
    assert r.status_code == 401
    assert r.json()["success"] is False


@case("AUTH-02")
def test_wrong_key_is_rejected(api, site):
    for header in ("Bearer nope", f"Bearer {API_KEY}x", API_KEY, f"Basic {API_KEY}"):
        r = httpx.post(api.base + "/v2/scrape", json={"url": site.url("/")}, headers={"Authorization": header})
        assert r.status_code == 401, header


@case("AUTH-03")
def test_health_needs_no_key(api):
    r = httpx.get(api.base + "/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@case("AUTH-05")
def test_unknown_paths_and_methods_get_json_errors(api):
    r = api.client.get("/nope")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    assert api.client.get("/v2/scrape").status_code == 405
    assert httpx.get(api.base + "/health").json() == {"status": "ok"}


@case("AUTH-04")
def test_every_other_route_needs_a_key(api):
    for method, path in [
        ("GET", "/v2/crawl/x"),
        ("POST", "/v2/map"),
        ("POST", "/v1/crawl"),
        ("POST", "/mcp"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    ]:
        assert httpx.request(method, api.base + path).status_code == 401, path


# --- AC-02 scrape ----------------------------------------------------------------


@case("SCR-01")
def test_scrape_markdown_main_content_and_metadata(api, site):
    r = api.client.post("/v2/scrape", json={"url": site.url("/")})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    data = body["data"]
    assert "Main content paragraph." in data["markdown"]
    assert "Footer text" not in data["markdown"]
    assert set(data) == {"markdown", "metadata"}
    meta = data["metadata"]
    assert meta["title"] == "Home Test"
    assert meta["description"] == "Home description"
    assert meta["language"] == "pt-BR"
    assert meta["ogTitle"] == "Home OG"
    assert meta["statusCode"] == 200
    assert meta["sourceURL"] == site.url("/")
    assert meta["url"].startswith(site.origin)
    assert meta["proxyUsed"] == "basic"


@case("SCR-02")
def test_scrape_full_content_and_other_formats(api, site):
    r = api.client.post(
        "/v2/scrape",
        json={
            "url": site.url("/"),
            "onlyMainContent": False,
            "formats": ["markdown", "html", "rawHtml", "links", {"type": "screenshot"}],
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert "Footer text" in data["markdown"]
    assert "Main content paragraph." in data["html"]
    assert "<footer>" in data["rawHtml"]
    assert site.url("/a") in data["links"]
    assert site.url("/b") in data["links"]
    assert "https://external.example.org/x" in data["links"]
    assert data["screenshot"].startswith("data:image/")


@case("SCR-03")
def test_scrape_reports_http_errors_as_success_with_status(api, site):
    data = api.client.post("/v2/scrape", json={"url": site.url("/missing"), "proxy": "basic"}).json()["data"]
    assert data["metadata"]["statusCode"] == 404
    assert "nothing here" in data["markdown"]


@case("SCR-04")
def test_bad_requests_return_400(api, site):
    assert api.client.post("/v2/scrape", json={"url": site.url("/"), "formats": ["json"]}).status_code == 400
    assert api.client.post("/v2/scrape", json={"formats": ["markdown"]}).status_code == 400
    assert api.client.post("/v2/scrape", json={"url": site.url("/"), "proxy": "magic"}).status_code == 400
    for bad in ("http://[::1", "http://exa%41mple.com/"):
        r = api.client.post("/v2/scrape", json={"url": bad})
        assert r.status_code == 400, (bad, r.text)


@case("SCR-05")
def test_malformed_links_on_a_page_are_skipped(api, site):
    r = api.client.post("/v2/scrape", json={"url": site.url("/badlinks"), "formats": ["markdown", "links"]})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["links"] == [site.url("/b")]
    assert api.client.post("/v2/map", json={"url": site.url("/badlinks"), "sitemap": "skip"}).status_code == 200


# --- AC-03 proxy policy ------------------------------------------------------------


@case("PRX-01")
def test_auto_goes_direct_when_not_blocked(api, site, proxy):
    before = len(proxy.requests)
    data = api.client.post("/v2/scrape", json={"url": site.url("/b")}).json()["data"]
    assert data["metadata"]["proxyUsed"] == "basic"
    assert len(proxy.requests) == before


@case("PRX-02")
def test_auto_retries_blocked_403_through_proxy(api, site, proxy):
    before = site.proxied_hits["/blocked"]
    data = api.client.post("/v2/scrape", json={"url": site.url("/blocked")}).json()["data"]
    assert data["metadata"]["proxyUsed"] == "stealth"
    assert data["metadata"]["statusCode"] == 200
    assert "Unblocked content via proxy" in data["markdown"]
    assert site.proxied_hits["/blocked"] == before + 1


@case("PRX-03")
def test_auto_retries_challenge_page_through_proxy(api, site):
    data = api.client.post("/v2/scrape", json={"url": site.url("/challenge")}).json()["data"]
    assert data["metadata"]["proxyUsed"] == "stealth"
    assert "Real content behind challenge" in data["markdown"]


@case("PRX-04")
def test_basic_never_uses_proxy(api, site, proxy):
    before = len(proxy.requests)
    data = api.client.post("/v2/scrape", json={"url": site.url("/blocked"), "proxy": "basic"}).json()["data"]
    assert data["metadata"]["proxyUsed"] == "basic"
    assert data["metadata"]["statusCode"] == 403
    assert len(proxy.requests) == before


@case("PRX-05")
def test_stealth_always_uses_proxy(api, site):
    before = site.proxied_hits["/b"]
    data = api.client.post("/v2/scrape", json={"url": site.url("/b"), "proxy": "stealth"}).json()["data"]
    assert data["metadata"]["proxyUsed"] == "stealth"
    assert site.proxied_hits["/b"] == before + 1


@case("PRX-06")
def test_without_proxy_auto_warns_and_stealth_fails(api_noproxy, site):
    data = api_noproxy.client.post("/v2/scrape", json={"url": site.url("/blocked")}).json()["data"]
    assert data["metadata"]["proxyUsed"] == "basic"
    assert data["metadata"]["statusCode"] == 403
    assert "no proxy configured" in data["warning"]
    r = api_noproxy.client.post("/v2/scrape", json={"url": site.url("/b"), "proxy": "stealth"})
    assert r.status_code == 400
    assert "no proxy" in r.json()["error"]


@case("PRX-07")
def test_enhanced_proxy_mode_is_stealth(api, site):
    data = api.client.post("/v2/scrape", json={"url": site.url("/b"), "proxy": "enhanced"}).json()["data"]
    assert data["metadata"]["proxyUsed"] == "stealth"


# --- AC-06 v1 --------------------------------------------------------------------


@case("V1-01")
def test_v1_scrape_shape(api, site):
    body = api.client.post("/v1/scrape", json={"url": site.url("/"), "formats": ["markdown", "screenshot@fullPage"]}).json()
    assert body["success"] is True
    assert "Main content paragraph." in body["data"]["markdown"]
    assert body["data"]["screenshot"].startswith("data:image/")


# --- AC-08 URL guard -----------------------------------------------------------------


@case("SSRF-01")
def test_guard_rejects_private_and_non_http_destinations(api_guarded, site):
    for url in [
        "http://127.0.0.1:5901/",
        f"http://127.0.0.1:{api_guarded.port}/health",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost/",
        site.url("/"),
        "file:///etc/passwd",
    ]:
        for path in ("/v2/scrape", "/v2/map", "/v2/crawl"):
            r = api_guarded.client.post(path, json={"url": url})
            assert r.status_code == 400, (path, url, r.text)
            assert r.json()["success"] is False


@case("SSRF-02")
def test_guard_allows_public_sites(api_guarded):
    r = api_guarded.client.post("/v2/scrape", json={"url": "https://example.com"})
    assert r.status_code == 200, r.text
    assert "Example Domain" in r.json()["data"]["metadata"].get("title", "")


@case("SSRF-03")
def test_guard_blocks_private_subresources_and_navigations(guarded_lan, site):
    canary = site.canary
    before = sum(canary.hits.values())
    r = guarded_lan.client.post("/v2/scrape", json={"url": site.url("/embeds-internal"), "formats": ["rawHtml", "markdown"]})
    assert r.status_code == 200, r.text
    assert "Embed page content" in r.json()["data"]["markdown"]
    r = guarded_lan.client.post("/v2/scrape", json={"url": site.url("/js-nav-internal")})
    assert "SECRET-CANARY" not in r.text
    assert sum(canary.hits.values()) == before, dict(canary.hits)


@case("SSRF-04")
def test_guard_drops_pages_that_redirect_to_private_addresses(guarded_lan, site):
    r = guarded_lan.client.post("/v2/scrape", json={"url": site.url("/redirect-internal")})
    assert r.status_code == 400, r.text
    assert "SECRET-CANARY" not in r.text
    assert "private" in r.json()["error"]


@case("SSRF-05")
def test_guard_rejects_urls_browsers_and_python_read_differently(api_guarded):
    for url in ["http://127.0.0.1:5901\\@example.com/", "http://user:pw@example.com/", "http://example.com@127.0.0.1/"]:
        r = api_guarded.client.post("/v2/scrape", json={"url": url})
        assert r.status_code == 400, (url, r.text)

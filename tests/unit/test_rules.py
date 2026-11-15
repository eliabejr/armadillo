"""Pure rules: block detection, URL guard, HTML parsing, formats, crawl scope, sitemaps, config."""

from __future__ import annotations

import gzip

import pytest

from scrape_api.blocking import blocked_reason, is_block_like_error
from scrape_api.config import Settings
from scrape_api.crawl import CrawlScope
from scrape_api.engine import proxy_config_from_url
from scrape_api.htmlinfo import extract_links, extract_metadata, normalize_url
from scrape_api.mapper import host_in_scope, parse_sitemap, robots_sitemaps
from scrape_api.models import InvalidRequest, parse_formats
from scrape_api.service import with_scheme
from scrape_api.urlguard import URLNotAllowed, canonical_url, check_url_sync, is_literal_private


# --- block detection -------------------------------------------------------


@pytest.mark.parametrize("status", [403, 429, 503])
def test_block_statuses(status):
    assert blocked_reason(status, "<html>ok</html>") == f"HTTP {status}"


def test_normal_pages_are_not_blocked():
    assert blocked_reason(200, "<html><title>Shop</title>hello</html>") is None
    assert blocked_reason(404, "<html>not found</html>") is None
    # Cloudflare injects this script on normal pages of protected sites.
    normal_cf = '<html><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>hi</html>'
    assert blocked_reason(200, normal_cf) is None


def test_challenge_pages_are_blocked():
    assert blocked_reason(200, "<script>window._cf_chl_opt={}</script>") is not None
    assert blocked_reason(200, "<html><head><title>Just a moment...</title></head></html>") is not None
    assert blocked_reason(200, '<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe>') is not None


def test_block_like_errors():
    assert is_block_like_error("net::ERR_CONNECTION_RESET at https://x")
    assert is_block_like_error("Timeout 30000ms exceeded")
    assert not is_block_like_error("net::ERR_NAME_NOT_RESOLVED")
    assert not is_block_like_error(None)


# --- URL guard ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:5901/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
    ],
)
def test_private_destinations_rejected(url):
    with pytest.raises(URLNotAllowed):
        check_url_sync(url, allow_private=False)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/", "gopher://x/", "http:///nohost"])
def test_bad_schemes_and_hosts_rejected_even_when_private_allowed(url):
    with pytest.raises(URLNotAllowed):
        check_url_sync(url, allow_private=True)


def test_public_ip_and_private_override_allowed():
    assert check_url_sync("http://1.1.1.1", allow_private=False) == "http://1.1.1.1/"
    assert check_url_sync("http://127.0.0.1:8080/x?y=1#z", allow_private=True) == "http://127.0.0.1:8080/x?y=1"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:5901\\@example.com/",  # Chromium reads the host as 127.0.0.1
        "http://user:pass@example.com/",
        "http://example.com@127.0.0.1/",
        "http://exa mple.com/",
        "http://example.com/\x00",
        "http://example.com:99999/",
        "http://example.com:port/",
        "http://[::1",
        "http://exa%41mple.com/",
        "http://exa<mple.com/",
    ],
)
def test_ambiguous_urls_rejected(url):
    with pytest.raises(URLNotAllowed):
        check_url_sync(url, allow_private=True)


def test_canonical_url_rebuilds_what_was_checked():
    assert canonical_url("HTTPS://BÜCHER.example:8443/a?b=1#c") == (
        "https://xn--bcher-kva.example:8443/a?b=1",
        "xn--bcher-kva.example",
        8443,
    )
    assert canonical_url("http://[2001:db8::1]/")[0] == "http://[2001:db8::1]/"


def test_is_literal_private():
    for url in ["http://127.0.0.1:5901/", "http://[::1]/", "http://169.254.169.254/x", "http://localhost/",
                "http://a.localhost/", "http://10.1.2.3/", "http://[::ffff:127.0.0.1]/"]:
        assert is_literal_private(url), url
    for url in ["https://example.com/", "http://1.1.1.1/", "data:text/plain,x", "about:blank"]:
        assert not is_literal_private(url), url


def test_with_scheme():
    assert with_scheme("example.com") == "https://example.com"
    assert with_scheme("example.com/?next=https://x") == "https://example.com/?next=https://x"
    assert with_scheme(" http://a.com ") == "http://a.com"


# --- HTML parsing ------------------------------------------------------------


def test_extract_links_absolute_deduplicated_and_filtered():
    html = """<html><head><base href="/docs/"></head><body>
      <a href="page">Page</a> <a href="page#frag">Same</a> <a href="https://other.org/x">Ext</a>
      <a href="mailto:a@b.c">m</a> <a href="javascript:void(0)">j</a> <a href="#top">t</a>
    </body></html>"""
    links = extract_links(html, "https://site.com/start")
    assert [link.url for link in links] == ["https://site.com/docs/page", "https://other.org/x"]
    assert links[0].text == "Page"


def test_extract_links_skips_malformed_urls():
    html = """<a href="http://localhost:PORT/">bad port</a> <a href="http://[::1/x">bad v6</a>
      <a href="https://site.com:99999/">out of range</a> <a href="/ok">ok</a>"""
    assert [link.url for link in extract_links(html, "https://site.com/")] == ["https://site.com/ok"]


def test_extract_links_handles_empty_html():
    assert extract_links("", "https://x.com") == []
    assert extract_links(None, "https://x.com") == []


def test_extract_metadata():
    html = """<html lang="pt-BR"><head><title> Hello   World </title>
      <meta name="description" content="Desc"><meta property="og:title" content="OG"></head></html>"""
    meta = extract_metadata(html)
    assert meta == {"title": "Hello World", "language": "pt-BR", "description": "Desc", "ogTitle": "OG"}


def test_normalize_url():
    assert normalize_url("HTTPS://Site.COM:443/a?b=1#c") == "https://site.com/a?b=1"
    assert normalize_url("http://site.com:8080", drop_query=True) == "http://site.com:8080/"
    assert normalize_url("https://site.com/a?b=1", drop_query=True) == "https://site.com/a"
    assert normalize_url("http://[2001:db8::1]:8080/x") == "http://[2001:db8::1]:8080/x"
    assert normalize_url("http://site.com:PORT/") is None
    assert normalize_url("ftp://site.com/") is None


# --- formats -----------------------------------------------------------------


def test_parse_formats_variants():
    assert parse_formats(None).names == {"markdown"}
    spec = parse_formats(["markdown", "raw_html", {"type": "screenshot", "fullPage": True}, "links"])
    assert spec.names == {"markdown", "rawHtml", "screenshot", "links"}
    assert parse_formats(["screenshot@fullPage"]).names == {"screenshot"}


@pytest.mark.parametrize("bad", [["json"], [{"type": "summary"}], [42]])
def test_parse_formats_rejects_unsupported(bad):
    with pytest.raises(InvalidRequest):
        parse_formats(bad)


# --- crawl scope -------------------------------------------------------------


def test_scope_child_paths_by_default():
    scope = CrawlScope.build("https://site.com/blog/", include_paths=[], exclude_paths=[])
    assert scope.allows("https://site.com/blog/post-1")
    assert scope.allows("https://www.site.com/blog/post-2")
    assert not scope.allows("https://site.com/about")
    assert not scope.allows("https://site.com/blogger")
    assert not scope.allows("https://other.com/blog/x")


def test_scope_entire_domain_subdomains_and_external():
    scope = CrawlScope.build("https://site.com/blog/", include_paths=[], exclude_paths=[], entire_domain=True)
    assert scope.allows("https://site.com/about")
    assert not scope.allows("https://docs.site.com/x")
    sub = CrawlScope.build("https://site.com/", include_paths=[], exclude_paths=[], allow_subdomains=True)
    assert sub.allows("https://docs.site.com/x")
    ext = CrawlScope.build("https://site.com/", include_paths=[], exclude_paths=[], allow_external=True)
    assert ext.allows("https://other.org/")


def test_scope_include_exclude_regex():
    scope = CrawlScope.build("https://site.com/", include_paths=["^/a"], exclude_paths=["deep"])
    assert scope.allows("https://site.com/a/page")
    assert not scope.allows("https://site.com/b")
    assert not scope.allows("https://site.com/a/deep")
    with pytest.raises(InvalidRequest):
        CrawlScope.build("https://site.com/", include_paths=["("], exclude_paths=[])
    with pytest.raises(InvalidRequest):
        CrawlScope.build("https://site.com/", include_paths=["a"] * 40, exclude_paths=[])


def test_host_in_scope():
    assert host_in_scope("www.site.com", "site.com", False)
    assert not host_in_scope("a.site.com", "site.com", False)
    assert host_in_scope("a.site.com", "www.site.com", True)
    assert not host_in_scope("evilsite.com", "site.com", True)


# --- sitemaps ----------------------------------------------------------------


def test_parse_sitemap_urlset_index_and_gzip():
    urlset = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc> https://s.com/a </loc></url><url><loc>https://s.com/b</loc></url></urlset>"""
    assert parse_sitemap(urlset) == (["https://s.com/a", "https://s.com/b"], [])
    index = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://s.com/s1.xml</loc></sitemap></sitemapindex>"""
    assert parse_sitemap(index) == ([], ["https://s.com/s1.xml"])
    assert parse_sitemap(gzip.compress(urlset))[0] == ["https://s.com/a", "https://s.com/b"]
    assert parse_sitemap(b"not xml at all") == ([], [])


def test_parse_sitemap_does_not_expand_entities():
    xxe = b"""<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/hostname">]>
      <urlset><url><loc>https://s.com/&e;</loc></url></urlset>"""
    pages, _ = parse_sitemap(xxe)
    assert pages in ([], ["https://s.com/"])  # the entity is left unexpanded


def test_robots_sitemaps():
    robots = "User-agent: *\nDisallow: /x\nSitemap: https://s.com/a.xml\nsitemap:https://s.com/b.xml\n"
    assert robots_sitemaps(robots) == ["https://s.com/a.xml", "https://s.com/b.xml"]


# --- config and proxies ------------------------------------------------------


def test_service_refuses_to_start_without_keys_or_with_bad_limits():
    with pytest.raises(RuntimeError):
        Settings().validate()
    with pytest.raises(RuntimeError):
        Settings(api_keys=("k",), max_crawl_jobs=0).validate()
    Settings(api_keys=("k",)).validate()


def test_proxy_config_from_url_decodes_credentials():
    cfg = proxy_config_from_url("http://us%40er:p%3Ass@proxy.example:8080")
    assert cfg.server == "http://proxy.example:8080"
    assert cfg.username == "us@er"
    assert cfg.password == "p:ss"
    with pytest.raises(ValueError):
        proxy_config_from_url("http://no-port.example")


def test_sitemap_filter_thread_stops_when_told():
    import threading

    from scrape_api.mapper import _accepted

    urlset = b"<urlset>" + b"".join(b"<url><loc>https://s.com/%d</loc></url>" % i for i in range(50)) + b"</urlset>"
    stop = threading.Event()
    assert len(_accepted(urlset, lambda u: True, 100, stop)[0]) == 50
    assert len(_accepted(urlset, lambda u: True, 10, stop)[0]) == 10
    stop.set()
    assert _accepted(urlset, lambda u: True, 100, stop)[0] == []

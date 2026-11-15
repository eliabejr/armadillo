"""Real servers for integration tests: a test site, a marking forward proxy and the API itself.

The site listens on the machine's non-loopback IP because Chromium never sends
loopback traffic through a proxy.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn

from scrape_api.config import Settings
from scrape_api.main import create_app

API_KEY = "test-key-123"
PROXY_MARK = "X-Test-Proxy"


def lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def page(title: str, body: str, lang: str = "en") -> str:
    return f'<!doctype html><html lang="{lang}"><head><title>{title}</title></head><body>{body}</body></html>'


HOME = """<!doctype html><html lang="pt-BR"><head><title>Home Test</title>
<meta name="description" content="Home description"><meta property="og:title" content="Home OG"></head>
<body><nav><a href="/a">A</a> <a href="/blog/">Blog</a></nav>
<main><h1>Welcome</h1><p>Main content paragraph.</p><a href="/b">B link</a>
<a href="https://external.example.org/x">External</a></main>
<footer>Footer text <a href="/private/secret">secret</a></footer></body></html>"""

CHALLENGE = """<!doctype html><html><head><title>Just a moment...</title></head>
<body><script>window._cf_chl_opt={cvId:'3'};</script>Checking your browser</body></html>"""

MANY = 60


def site_pages(origin: str, canary: str) -> dict[str, tuple[int, str, str]]:
    sitemap_index = f"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>{origin}/sitemap-pages.xml</loc></sitemap></sitemapindex>"""
    urls = "".join(f"<url><loc>{origin}{p}</loc></url>" for p in ["/", "/a", "/b", "/blog/1", "/sitemap-only-page"])
    # Real sitemaps contain junk; it must be skipped, not break map or crawl.
    urls += "<url><loc>http://localhost:PORT/x</loc></url><url><loc>http://[::1/broken</loc></url>"
    sitemap_pages = f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    many_links = "".join(f'<a href="/many/{i}">item {i}</a> ' for i in range(MANY))
    pages = {
        "/": (200, "text/html", HOME),
        "/a": (200, "text/html", page("Page A", '<p>Page A content</p><a href="/a/deep">deep</a>')),
        "/a/deep": (200, "text/html", page("Deep A", '<p>Deep A content</p><a href="/a/deeper">deeper</a>')),
        "/a/deeper": (200, "text/html", page("Deeper A", "<p>Deeper A content</p>")),
        "/b": (200, "text/html", page("Page B", "<p>Page B content</p>")),
        "/blog/": (200, "text/html", page("Blog", '<a href="/blog/1">one</a> <a href="/blog/2">two</a> <a href="/a">A</a>')),
        "/blog/1": (200, "text/html", page("Blog 1", "<p>Blog post one</p>")),
        "/blog/2": (200, "text/html", page("Blog 2", "<p>Blog post two</p>")),
        "/private/secret": (200, "text/html", page("Secret", "<p>secret page</p>")),
        "/sitemap-only-page": (200, "text/html", page("Sitemap only", "<p>Only in sitemap</p>")),
        "/missing": (404, "text/html", page("Not found", "<p>nothing here</p>")),
        "/many/": (200, "text/html", page("Many", many_links)),
        "/badlinks": (
            200,
            "text/html",
            page("Bad links", '<p>Bad links page</p><a href="http://localhost:PORT/">x</a> <a href="http://[::1/y">y</a> '
                 '<a href="https://h.example:99999/">z</a> <a href="/b">good</a>'),
        ),
        "/embeds-internal": (
            200,
            "text/html",
            page("Embeds", f'<p>Embed page content</p><img src="{canary}/pixel.png">'
                 f'<iframe src="{canary}/frame"></iframe><script>fetch("{canary}/fetch").catch(() => 0)</script>'),
        ),
        "/js-nav-internal": (
            200,
            "text/html",
            page("JS nav", f'<p>Before nav</p><script>location.href = "{canary}/secret";</script>'),
        ),
        "/robots.txt": (200, "text/plain", f"User-agent: *\nDisallow: /private/\nSitemap: {origin}/sitemap.xml\n"),
        "/sitemap.xml": (200, "application/xml", sitemap_index),
        "/sitemap-pages.xml": (200, "application/xml", sitemap_pages),
    }
    for i in range(MANY):
        pages[f"/many/{i}"] = (200, "text/html", page(f"Item {i}", f"<p>Item {i} content</p>"))
    return pages


class Canary:
    """A loopback-only server the API must never reach when private URLs are blocked."""

    def __init__(self):
        self.hits: Counter[str] = Counter()
        canary = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                canary.hits[urlsplit(self.path).path] += 1
                data = page("Canary", "<p>SECRET-CANARY</p>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class Site:
    def __init__(self):
        self.ip = lan_ip()
        self.hits: Counter[str] = Counter()
        self.proxied_hits: Counter[str] = Counter()
        self.canary = Canary()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                via_proxy = self.headers.get(PROXY_MARK) == "1"
                site.hits[path] += 1
                if via_proxy:
                    site.proxied_hits[path] += 1
                if path == "/blocked":
                    status, ctype, body = (
                        (200, "text/html", page("Unblocked", "<p>Unblocked content via proxy</p>"))
                        if via_proxy
                        else (403, "text/html", CHALLENGE)
                    )
                elif path == "/redirect-internal":
                    self.send_response(302)
                    self.send_header("Location", site.canary.origin + "/secret")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                elif path == "/challenge":
                    status, ctype, body = (
                        (200, "text/html", page("Real", "<p>Real content behind challenge</p>"))
                        if via_proxy
                        else (200, "text/html", CHALLENGE)
                    )
                else:
                    status, ctype, body = site.pages.get(path, (404, "text/html", page("404", "nope")))
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", f"{ctype}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self.server.server_address[1]
        self.origin = f"http://{self.ip}:{self.port}"
        self.pages = site_pages(self.origin, self.canary.origin)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return self.origin + path


class MarkingProxy:
    """Plain-HTTP forward proxy that adds X-Test-Proxy: 1 to every request it relays."""

    def __init__(self):
        self.requests: list[str] = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                target = urlsplit(self.path)
                proxy.requests.append(self.path)
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("proxy-connection", "connection")}
                headers[PROXY_MARK] = "1"
                conn = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=20)
                path = target.path + (f"?{target.query}" if target.query else "")
                conn.request("GET", path or "/", headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                conn.close()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class Api:
    def __init__(self, settings: Settings):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.app = create_app(settings)
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(self.base + "/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError("API did not become healthy")
        self.client = httpx.Client(
            base_url=self.base, headers={"Authorization": f"Bearer {API_KEY}"}, timeout=120
        )

    def stop(self):
        self.client.close()
        self.server.should_exit = True
        self.thread.join(timeout=30)


@pytest.fixture(scope="session")
def site():
    s = Site()
    yield s
    s.server.shutdown()
    s.canary.server.shutdown()


@pytest.fixture(scope="session")
def proxy():
    p = MarkingProxy()
    yield p
    p.server.shutdown()


def _api(**overrides) -> Api:
    return Api(Settings(api_keys=(API_KEY,), max_concurrency=4, page_timeout_ms=20_000, **overrides))


@pytest.fixture(scope="session")
def api(proxy):
    """Proxy configured, private URLs allowed (to reach the test site)."""
    a = _api(proxy_urls=(proxy.url,), allow_private_urls=True)
    yield a
    a.stop()


@pytest.fixture(scope="session")
def api_noproxy():
    a = _api(allow_private_urls=True)
    yield a
    a.stop()


@pytest.fixture(scope="session")
def api_guarded():
    """Production defaults: no proxy, private destinations rejected."""
    a = _api()
    yield a
    a.stop()


@pytest.fixture
def guarded_lan(api_guarded, site, monkeypatch):
    """api_guarded, but treating the test site's LAN IP as public (loopback stays private).

    The API runs in this process, so patching the guard's address check applies to it.
    """
    from scrape_api import urlguard

    original = urlguard._is_public
    monkeypatch.setattr(urlguard, "_is_public", lambda address: address == site.ip or original(address))
    yield api_guarded


def wait_crawl(client: httpx.Client, status_url: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(status_url).json()
        if body.get("status") in ("completed", "failed", "cancelled"):
            return body
        time.sleep(0.5)
    raise AssertionError(f"crawl did not finish: {body}")

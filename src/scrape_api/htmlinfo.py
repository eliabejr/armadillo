"""Links and metadata read straight from the raw HTML (independent of Crawl4AI's own parsing)."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import lxml.html
from lxml import etree

MAX_LINKS = 10_000


@dataclass(frozen=True)
class Link:
    url: str
    text: str


def _parse(html: str | None):
    if not html or not html.strip():
        return None
    try:
        return lxml.html.fromstring(html)
    except (etree.ParserError, ValueError):
        return None


def normalize_url(url: str, *, drop_query: bool = False) -> str | None:
    """Absolute http(s) URL without fragment, lowercase scheme/host, no default port.

    Returns None for URLs that do not parse (bad port, broken IPv6 literal...),
    which pages and sitemaps contain more often than one would hope.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    query = "" if drop_query else parts.query
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def _links(root, base_url: str) -> list[Link]:
    base = base_url
    base_el = root.find(".//base[@href]")
    if base_el is not None:
        try:
            base = urljoin(base_url, base_el.get("href", "").strip())
        except ValueError:
            pass
    seen: set[str] = set()
    links: list[Link] = []
    for anchor in root.iter("a"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        try:
            absolute = normalize_url(urljoin(base, href))
        except ValueError:
            absolute = None
        if absolute is None or absolute in seen:
            continue
        seen.add(absolute)
        links.append(Link(url=absolute, text=" ".join(anchor.text_content().split())[:500]))
        if len(links) >= MAX_LINKS:
            break
    return links


_META_KEYS = {
    "description": "description",
    "keywords": "keywords",
    "author": "author",
    "og:title": "ogTitle",
    "og:description": "ogDescription",
    "og:image": "ogImage",
    "og:url": "ogUrl",
    "og:site_name": "ogSiteName",
}


def _metadata(root) -> dict[str, str]:
    meta: dict[str, str] = {}
    title = root.find(".//title")
    if title is not None and title.text_content().strip():
        meta["title"] = " ".join(title.text_content().split())
    lang = root.get("lang") if root.tag == "html" else None
    if lang is None:
        html_el = root.find(".//html")
        lang = html_el.get("lang") if html_el is not None else None
    if lang:
        meta["language"] = lang.strip()
    for el in root.iter("meta"):
        key = (el.get("name") or el.get("property") or "").strip().lower()
        content = el.get("content")
        if key in _META_KEYS and content and _META_KEYS[key] not in meta:
            meta[_META_KEYS[key]] = content.strip()
    return meta


def parse_page(html: str | None, base_url: str) -> tuple[list[Link], dict[str, str]]:
    """Links and metadata from one parse of the page."""
    root = _parse(html)
    if root is None:
        return [], {}
    return _links(root, base_url), _metadata(root)


def extract_links(html: str | None, base_url: str) -> list[Link]:
    """All <a href> targets as absolute http(s) URLs, in page order, deduplicated."""
    return parse_page(html, base_url)[0]


def extract_metadata(html: str | None) -> dict[str, str]:
    """Title, description, language and the common Open Graph fields."""
    return parse_page(html, "http://invalid/")[1]

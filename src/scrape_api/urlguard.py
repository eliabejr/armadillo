"""Reject URLs that would make the service fetch from its own private network.

`check_url` returns a canonical URL rebuilt from the parts it checked. Callers
must fetch that string, so Python and Chromium cannot read a different host out
of the same input (e.g. `http://127.0.0.1\\@example.com/`).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit


class URLNotAllowed(ValueError):
    pass


# Backslashes, whitespace and control characters are parsed differently by
# browsers (WHATWG) and urllib; none belong in a URL an agent sends us.
_AMBIGUOUS = re.compile(r"[\\\s\x00-\x1f\x7f]")
_HOST_LABEL = re.compile(r"^[a-z0-9_-]{1,63}$")


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def canonical_url(url: str) -> tuple[str, str, int]:
    """Return (canonical URL, ASCII host, port) or raise URLNotAllowed."""
    url = url.strip()
    if _AMBIGUOUS.search(url):
        raise URLNotAllowed("URL contains backslashes, spaces or control characters")
    try:
        parts = urlsplit(url)
    except ValueError as exc:  # e.g. "http://[::1"
        raise URLNotAllowed("malformed URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise URLNotAllowed("only http and https URLs are allowed")
    if "@" in parts.netloc:
        raise URLNotAllowed("URLs with credentials are not allowed")
    host = parts.hostname
    if not host:
        raise URLNotAllowed("URL has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise URLNotAllowed("URL has an invalid port") from exc
    if _ip(host) is not None:
        ascii_host = host
        netloc = f"[{host}]" if ":" in host else host
    else:
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise URLNotAllowed(f"invalid host name {host!r}") from exc
        if not all(_HOST_LABEL.match(label) for label in ascii_host.rstrip(".").split(".")):
            raise URLNotAllowed(f"invalid host name {host!r}")
        netloc = ascii_host
    if port is not None:
        netloc += f":{port}"
    canonical = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
    return canonical, ascii_host, port or (443 if scheme == "https" else 80)


def check_url_sync(url: str, allow_private: bool) -> str:
    """Return the canonical URL if it may be fetched, else raise URLNotAllowed."""
    canonical, host, port = canonical_url(url)
    if allow_private:
        return canonical
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise URLNotAllowed(f"cannot resolve host {host!r}") from exc
    for info in infos:
        if not _is_public(str(info[4][0])):
            raise URLNotAllowed(f"host {host!r} resolves to a private or reserved address")
    return canonical


async def check_url(url: str, allow_private: bool) -> str:
    return await asyncio.to_thread(check_url_sync, url, allow_private)


def is_literal_private(url: str) -> bool:
    """True for URLs whose host is a non-public IP literal or a localhost name (no DNS lookup).

    The browser's request guard uses this. Names that resolve to private
    addresses are stopped by the systemd unit's IPAddressDeny at connect time.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return True
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    return _ip(host) is not None and not _is_public(host)

"""Decide whether a direct fetch was blocked, so `proxy: auto` can retry via a proxy."""

from __future__ import annotations

BLOCK_STATUSES = frozenset({403, 429, 503})

# Markers that appear on bot-challenge or block pages, not on normal pages of
# protected sites (e.g. Cloudflare's jsd script is on normal pages too).
CHALLENGE_MARKERS = (
    "_cf_chl_opt",  # Cloudflare challenge
    "<title>Attention Required! | Cloudflare</title>",
    "<title>Just a moment...</title>",
    "captcha-delivery.com",  # DataDome block page
    'id="px-captcha"',  # PerimeterX / HUMAN
    "Request unsuccessful. Incapsula incident ID",
    "errors.edgesuite.net",  # Akamai "Access Denied"
)

# Chromium network errors that usually mean the site dropped our IP.
BLOCK_LIKE_ERRORS = (
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_EMPTY_RESPONSE",
    "ERR_TIMED_OUT",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_HTTP2_PROTOCOL_ERROR",
    "Timeout",
)

_SCAN_BYTES = 50_000


def blocked_reason(status_code: int | None, html: str | None) -> str | None:
    """Return why a page looks blocked, or None."""
    if status_code in BLOCK_STATUSES:
        return f"HTTP {status_code}"
    head = (html or "")[:_SCAN_BYTES]
    for marker in CHALLENGE_MARKERS:
        if marker in head:
            return f"challenge page ({marker})"
    return None


def is_block_like_error(message: str | None) -> bool:
    return bool(message) and any(code in message for code in BLOCK_LIKE_ERRORS)

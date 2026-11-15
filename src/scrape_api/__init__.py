"""Self-hosted, Firecrawl-compatible scraping API and MCP server on top of Crawl4AI."""

import os

# Set before Crawl4AI is imported: it prints an ad for its cloud when it thinks a page is blocked.
os.environ.setdefault("CRAWL4AI_NO_CLOUD_NOTICE", "1")

# armadillo

**English** · [Português](README.pt-BR.md) · [Español](README.es.md)

A self-hosted scraping API with the same API as Firecrawl, plus an MCP server
for Claude agents. It runs on [Crawl4AI](https://github.com/unclecode/crawl4ai)
(Chromium through Playwright) and fits in a single systemd service.

On the server and in the code the name is `scrape-api` (systemd service,
`/etc/scrape-api`, Python package `scrape_api`).

- **Firecrawl-shaped REST**: `/v2/scrape`, `/v2/map`, `/v2/crawl` (and the
  `/v1/*` aliases). The official `firecrawl-py` SDK works by changing only the
  URL.
- **MCP at `/mcp`** (streamable HTTP), with the tools `scrape`, `screenshot`,
  `map`, `crawl_start` and `crawl_status`.
- **API key** required (`Authorization: Bearer <key>`) everywhere except
  `/health`.
- **IP proxy only when needed**: requests go out directly and retry through a
  proxy only if they look blocked.

Requirements, decisions and acceptance criteria: [docs/plan.md](docs/plan.md).

## Connect an agent

In the examples below, `<URL>` is the service's public URL (see
[Access from other machines](#access-from-other-machines)).

Get the key on the service's machine:

```bash
sudo sed -n 's/^SCRAPE_API_KEYS=//p' /etc/scrape-api/env
```

**Claude Code** (on any machine):

```bash
claude mcp add --scope user --transport http scrape <URL>/mcp \
  --header "Authorization: Bearer <key>"
```

**Firecrawl SDK** (Python):

```python
from firecrawl import Firecrawl
fc = Firecrawl(api_key="<key>", api_url="<URL>")
doc = fc.scrape("https://example.com", formats=["markdown"])
job = fc.crawl("https://docs.example.com", limit=50)
```

**curl**:

```bash
curl -s <URL>/v2/scrape \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "formats": ["markdown", "links"]}'
```

## Endpoints

| Route | What it does |
| --- | --- |
| `GET /health` | Status, no authentication. |
| `POST /v2/scrape` | One page. `formats`: `markdown`, `html`, `rawHtml`, `links`, `screenshot` (always the full page; `fullPage` is accepted and ignored). Also `onlyMainContent` (default `true`), `includeTags`, `excludeTags`, `waitFor` (ms), `timeout` (ms), `proxy`. |
| `POST /v2/map` | The site's URLs, from robots.txt/sitemaps and the page's links. Also `search`, `limit`, `sitemap` (`include`/`skip`/`only`), `includeSubdomains`, `ignoreQueryParameters`. |
| `POST /v2/crawl` | Starts a crawl and returns `id` and `url`. Also `limit`, `maxDiscoveryDepth`, `includePaths`/`excludePaths` (regex on the path), `crawlEntireDomain`, `allowSubdomains`, `allowExternalLinks`, `sitemap`, `delay`, `maxConcurrency`, `scrapeOptions`. Respects robots.txt. |
| `GET /v2/crawl/{id}` | `status`, `total`, `completed` and `data`, in pages of up to 50 documents or 10 MB, with `next`. Includes a `warning` if the crawl stopped at its memory cap. |
| `GET /v2/crawl/{id}/errors` | `errors` and `robotsBlocked`. |
| `DELETE /v2/crawl/{id}` | Cancels. Returns 409 if the crawl has already finished. |
| `/v1/scrape`, `/v1/map`, `/v1/crawl…` | The v1 shape (`map` returns the URLs as strings; accepts `maxDepth` and `ignoreSitemap`). |
| `/mcp` | MCP server. |

Out of scope: `/search`, LLM extraction (`json`, `summary`), batch scrape,
webhooks and page actions.

## IP proxy

Each request's `proxy` field picks the path:

- `auto` (default): goes out directly. It retries through a configured proxy
  only if the direct response is a 403, 429 or 503, an anti-bot challenge page
  (Cloudflare, DataDome, PerimeterX, Imperva, Akamai), or if the connection
  drops or times out.
- `basic`: never uses a proxy.
- `stealth` (or `enhanced`, the name the newer SDK uses): always uses a proxy.
  Returns 400 if no proxy is configured.

`metadata.proxyUsed` says which path was used (`basic` or `stealth`). With no
proxy configured, a blocked page comes back with a `warning`.

To configure proxies, edit `/etc/scrape-api/env` and restart the service:

```bash
# One or more, comma-separated (used in rotation)
SCRAPE_PROXY_URLS=http://user:password@p.webshare.io:80
sudo systemctl restart scrape-api
```

Free option: [Webshare](https://www.webshare.io) has a free plan with 10
datacenter proxies and 1 GB/month. Some VM providers block outbound ports, so
check that the proxy's port gets through: Webshare uses 80, Bright Data 33335,
IPRoyal 12321 and Oxylabs 7777.

## Configuration (`/etc/scrape-api/env`)

| Variable | Default | |
| --- | --- | --- |
| `SCRAPE_API_KEYS` | (generated at install) | Accepted keys, comma-separated. The service does not start without one. |
| `SCRAPE_PROXY_URLS` | empty | Upstream proxies. |
| `SCRAPE_PORT` / `SCRAPE_HOST` | `3002` / `127.0.0.1` | Where the service listens. |
| `SCRAPE_PUBLIC_URL` | empty (the installer's `--public-url`) | Base of the crawls' `url`/`next` links. When empty, the request's address is used. |
| `SCRAPE_MAX_CONCURRENCY` | `4` | Pages open in the browser at the same time. |
| `SCRAPE_PAGE_TIMEOUT_MS` | `30000` | Timeout per page. |
| `SCRAPE_MAX_CRAWL_PAGES` | `500` | Cap on a crawl's `limit`. |
| `SCRAPE_CRAWL_TTL_SECONDS` / `SCRAPE_MAX_CRAWL_JOBS` | `86400` / `50` | How long a crawl stays available after it finishes, and how many are kept in memory. |
| `SCRAPE_MAX_CRAWL_MB` | `256` | Memory cap for one crawl's results. When it is reached, the crawl ends with a `warning`. |
| `SCRAPE_MAX_TOTAL_CRAWL_MB` | `1024` | Combined cap for all crawls. The oldest finished crawls are dropped first. |
| `SCRAPE_ALLOW_PRIVATE_URLS` | `false` | Allows private IPs and loopback. Keep it off on a public service. |

## Install on a new machine

It works on any Linux with systemd, cgroup v2 and the system's `python3` (3.10
or newer), run as a regular user with sudo. On Debian and Ubuntu the installer
installs Chromium's system libraries itself; on other distributions, install
them with the package manager if it asks for them.

```bash
git clone https://github.com/eliabejr/armadillo.git ~/apps/scrape-api
cd ~/apps/scrape-api && ./deploy/install.sh
```

The installer sets up:
- `uv` (if missing) and the Python environment;
- Playwright's Chromium in `/opt/scrape-api` (and the system libraries, if
  needed);
- the configuration file, with a new key;
- the systemd units `scrape-api` (the service) and `scrape-api-addresses` (the
  watcher of the machine's addresses, see [Security](#security)).

Then it checks `/health` and the network filter. Rerun it as often as you like:
the key and the settings are kept. Options: `--port N` and `--public-url URL`.

## Access from other machines

The service listens on `127.0.0.1:3002` only. For agents on other machines to
reach it, put an HTTPS reverse proxy or tunnel in front of it (the key travels
in a header), with a timeout of at least 2 minutes. For example:

```bash
tailscale serve --bg 3002                                        # only your machines (tailnet)
caddy reverse-proxy --from api.example.com --to 127.0.0.1:3002   # your own domain
cloudflared tunnel --url http://127.0.0.1:3002                   # no open ports
```

The VM provider's port publishing works too. Then record the public URL, which
becomes the base of the crawls' links:

```bash
./deploy/install.sh --public-url https://api.example.com
```

## From your workstation (this repo)

The target goes in `.deploy.env` (ignored by Git), for example
`SCRAPE_SSH="ssh -p 2222 user@host"`, or in the `SCRAPE_SSH` variable.

```bash
scripts/deploy.sh          # ships a new release to ~/apps/scrape-api/releases/ and runs install.sh
scripts/test-remote.sh     # runs the test suite on the remote machine (it has the browser)
SCRAPE_SSH="ssh user@other-machine" scripts/deploy.sh   # another machine
```

A new release goes into service only if its `install.sh` passes. If it fails,
the installer goes back to the previous release when that one passes the same
checks (the address watcher's included), or removes the units. The deploy keeps
the current and the previous release and deletes older ones.

The integration tests run the real API against a local test site and a local
test proxy. Each integration test has a stable ID
(`@pytest.mark.case("PRX-02")`), and `tests/conftest.py` fails if an ID is
missing or repeated.

## Security

Scraped pages are untrusted. Any site can try to lead the browser into the VM,
for example to a service on `localhost`, so the protections come in layers:

- **In the kernel (the layer that counts).**
  - The `scrape-api.socket` unit owns the port and hands it to the service
    through socket activation.
  - The service has `IPAddressDeny` for loopback, private networks,
    link-local, metadata and the machine's own prefixes, public ones included:
    its IPv4 addresses and the /64 around each IPv6 address (or the local
    network's prefix, if that is wider).
  - Every connection the service or Chromium opens to those destinations is
    dropped by the kernel (it times out), whether it comes from a redirect,
    iframe, popup, worker, websocket or DNS rebinding. Connections that arrive
    through the passed-in port are not affected.
  - Addresses change (a VM may wake up with another IPv6, without restarting
    anything), so the `scrape-api-addresses` unit follows the kernel's address
    and route events and adds each new prefix to the running service's filter,
    in under a second. It runs as root, from a copy of `deploy/netcheck.py`
    that only root can change, with the system's `python3`. The service is
    bound to it (`BindsTo`): if the watcher stops, the service stops.
- **In the application.** Ambiguous URLs are rejected: `\`, whitespace,
  credentials, an invalid port or host. The browser gets the canonical URL that
  was checked. Private hosts are rejected on the first hop. Browser requests to
  private IP literals or `localhost` are aborted. A page whose final URL is
  private is dropped, with a 400 response.
- **Isolation.**
  - Chromium runs with its sandbox on, as a dynamic user.
  - That user cannot see the login user's home (where the agents' credentials
    live), other users' processes or the Docker socket.
  - `MemoryMax=60%` and `TasksMax` apply too.
- **Caller input.** The `includePaths`/`excludePaths` regexes are time-limited:
  a slow pattern makes the crawl stop following links, with a `warning`. Proxy
  credentials are removed from error messages.

## Known limits

- **Machines that suspend when idle** (some sandbox providers do this) leave
  the public URL unanswered until they wake up. To keep the API always
  available, turn off auto-suspend at the provider.
- Crawls live in memory and are lost when the service restarts (Firecrawl also
  keeps results for only 24 h).
- **Kernel protection only with the installer.** It comes from the systemd unit
  and needs cgroup v2 with BPF. The installer checks the filter (from inside
  the service's cgroup, no connection to loopback or to the machine's addresses
  may get through) and, if it fails, goes back to the previous version only if
  that one passes the same check; otherwise it removes the units. Outside the
  unit (in the tests, for example), only the application's checks apply.
- **DNS.** Resolvers on a private network listed in `/etc/resolv.conf` (the
  VPC's, the router's) are allowed for the service, on all ports. Resolvers on
  loopback or link-local never are: the installer warns, and the service has
  no DNS until there is another one. With systemd-resolved, the service reads
  the servers from `/run/systemd/resolve/resolv.conf` instead of the
  `127.0.0.53` stub.
- **DNS fixed at install time.** Resolvers are read when the installer runs; if
  they change, run the installer again. The machine's addresses are not: the
  watcher follows them, with a window of a fraction of a second between a
  change and the block.
- **Neighbours on the same IPv6 network.** Sites hosted in the same /64 as the
  machine (or on the same local network, if its prefix is wider) are out of
  the service's reach.
- **Depth differs from Firecrawl.** Sitemap pages count as depth 1. The v1
  `maxDepth` counts link hops, not path segments. URLs blocked by robots.txt
  also count toward the `limit`.
- **Heavy anti-bot protection.** Sites with heavy anti-bot protection may keep
  blocking, even through a datacenter proxy. In those cases a residential proxy
  or Firecrawl Cloud works better.

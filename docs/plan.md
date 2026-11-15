# Plan: armadillo (scrape-api)

This file owns the requirements, acceptance criteria and plan for armadillo, whose
service and package are named scrape-api.

## Outcome

A self-hosted scraping API that Claude agents on any of the owner's machines can
call. It runs on a Debian sandbox VM (no public inbound except HTTPS through
the provider's port publishing), is reinstallable on other Linux hosts with
systemd with one script that does not depend on the hosting provider, and keeps
the Firecrawl API shape where that is practical.

Source of the requirements: the owner's requests in the 2026-11-14 session
("API proxy", repo kept on the Mac mini, endpoints reachable by Claude agents on
any machine, IP proxy only when needed, Firecrawl-compatible where viable).

## Scope

In scope: scrape, map and crawl over REST (`/v2/*` primary, `/v1/*` aliases);
a remote MCP endpoint for Claude agents; API-key auth; an optional pool of
upstream IP proxies with a direct-first policy; SSRF guard; install and deploy
scripts; deployment to the current sandbox.

Out of scope: `/search`, LLM extraction (`json`, `summary` formats), batch
scrape, webhooks, page actions, crawl jobs that survive a restart, multi-user
accounts or billing.

## Acceptance criteria

| ID | Observable result | Basis |
| --- | --- | --- |
| AC-01 | Every route except `GET /health` returns 401 `{success:false}` without a valid `Authorization: Bearer <key>`. The service refuses to start with no key configured. | Public URL; owner wants agents on any machine. |
| AC-02 | `POST /v2/scrape` returns `{success:true, data:{...}}` with `markdown` (main content by default, nav/header/footer removed), and `html`, `rawHtml`, `links`, `screenshot` when requested. `metadata` has `title`, `description`, `language`, `sourceURL`, `url`, `statusCode`, `proxyUsed`. | Firecrawl v2 scrape contract; firecrawl-py `Document`. |
| AC-03 | `proxy` is `basic`, `stealth` or `auto` (default `auto`). `basic` goes direct. `stealth` goes through a configured proxy, or returns 400 when none is configured. `auto` goes direct and retries through a proxy only when the direct attempt looks blocked (403/429/503, a bot-challenge page, or a connection reset/timeout). `metadata.proxyUsed` reports the path actually used. | Owner: proxy only when needed. Firecrawl v2 `proxy` option. |
| AC-04 | `POST /v2/map` returns `{success:true, links:[{url, title?}]}` built from robots.txt/sitemaps and the page's links. It keeps the site's host (and subdomains when `includeSubdomains`), applies `search` and `limit`, and drops fragments and query strings when `ignoreQueryParameters`. | Firecrawl v2 map. |
| AC-05 | `POST /v2/crawl` returns `{success:true, id, url}`. `GET /v2/crawl/{id}` returns `status`, `total`, `completed`, `data` and a `next` link while more results remain. `DELETE` cancels, or answers 409 once the crawl has finished. The crawl obeys `limit`, `maxDiscoveryDepth`, `includePaths`/`excludePaths` (regex on the path), child paths unless `crawlEntireDomain`, and robots.txt. The official firecrawl-py `crawl()` finishes against it. | Firecrawl v2 crawl; firecrawl-py 4.57. |
| AC-06 | `/v1/scrape`, `/v1/map` (links as strings) and `/v1/crawl` accept the v1 shapes. | Firecrawl v1, for older integrations. |
| AC-07 | `/mcp` is a streamable-HTTP MCP server behind the same auth, with the tools `scrape`, `screenshot`, `map`, `crawl_start` and `crawl_status`. An MCP client over HTTP lists the tools and calls them. | Claude agents on any machine (`claude mcp add --transport http`). |
| AC-08 | Unless `SCRAPE_ALLOW_PRIVATE_URLS=true`, the service never returns content from a loopback, private, link-local or otherwise non-global address. It rejects (400) URLs that are not http(s), that are ambiguous (backslash, whitespace, credentials, bad port), or whose host resolves to such an address. The browser gets the canonical URL that was checked. Browser requests to private IP literals or localhost are aborted, and a page whose final URL is private is dropped with 400. Under the installed systemd unit, any connection the service or its browser opens to loopback, private networks or the machine's own addresses (public ones included) is dropped by the kernel. Only DNS resolvers on a private network are allowed; loopback and link-local resolvers never are. The installer proves the filter from inside the service's cgroup and never leaves a unit running that failed that proof. Socket activation keeps inbound connections working. The browser runs sandboxed as a dynamic user that cannot read the login user's home, other users' processes or Docker. | Public service on a VM with internal services; the pages scraped are untrusted. |
| AC-09 | `deploy/install.sh` runs repeatedly on a Debian host. It installs the env, the Playwright Chromium and a systemd unit, generates an API key once and keeps it on reruns, waits for `/health`, and records the public URL given with `--public-url`. It depends on no hosting provider; on non-Debian distributions it asks for Chromium's system libraries instead of installing them. | Owner: "reinstall on other machines"; 2026-11-15: an installer agnostic to the provider. |
| AC-10 | The service runs on the current sandbox and answers from the Mac through the public HTTPS URL with the key. | Owner: endpoints reachable from any machine. |

## Decisions

- Build on Crawl4AI (already installed and tested on the box) instead of
  self-hosting Firecrawl. Self-hosted Firecrawl ships without auth, without
  Fire-engine and with six services. The owner accepted this on 2026-11-14.
- The service listens on 127.0.0.1 only. Expose it through an HTTPS proxy or
  tunnel in front (the provider's port publishing on the current host) and
  protect it with bearer API keys.
- MCP crawl is asynchronous (`crawl_start` and `crawl_status`) because the
  HTTPS proxy in front may time out long requests.
- Crawl jobs live in memory with a TTL (default 24 h) and are lost on restart,
  as with Firecrawl's 24 h retention.

## Slices

1. **Core API**: config, auth, SSRF guard, block detection, the Crawl4AI engine
   with the proxy policy, and `/v2` and `/v1` scrape. Tests: unit (block
   detection, URL guard, link and metadata extraction) and integration (AUTH,
   SCR, SSRF cases against a local test site and a local test proxy). Ends when
   these pass on the sandbox.
2. **Map and crawl**: sitemap and link discovery, the crawl job manager, and the
   `/v2` and `/v1` routes. Tests: MAP and CRW cases, plus SDK cases with
   firecrawl-py. Ends when these pass.
3. **MCP**: the FastMCP server mounted at `/mcp` behind auth. Tests: MCP cases
   with the official `mcp` client over HTTP. Ends when these pass.
4. **Install and deploy**: `install.sh`, `scripts/deploy.sh`,
   `scripts/test-remote.sh` and the README. Deploy to the sandbox, publish the
   port, call it from the Mac (REST, MCP and firecrawl-py) and check a rerun
   keeps the key. Ends when AC-09 and AC-10 hold.

## Verification

- Tests run on the sandbox (`scripts/test-remote.sh`), which has the browser.
  The Mac only holds the repo.
- Integration tests start the real app with uvicorn on a random port. They use
  a local test site on the machine's non-loopback IP and a local forward proxy
  that marks proxied requests. Chromium never proxies loopback, so the target
  must not be on 127.0.0.1.
- Test-case IDs: `@pytest.mark.case("ID")` on every integration test. A
  conftest hook fails collection on a missing or duplicate ID.
- Defect detection: mutate auth, the auto-proxy rule and the SSRF guard on a
  throwaway copy and show that the matching cases fail.
- Risks:
  - Crawl4AI's per-run `proxy_config` might be ignored by a shared browser.
    SCR-03 and SCR-05 would show it.
  - The HTTPS proxy in front might strip `Authorization` or time out. The AC-10 run
    from the Mac would show it.
  - Auto-suspend might take the URL down. A wake test checks it, and the
    result is reported.

## Review

Three fresh reviewers: behaviour (spec, bugs), structure (standards,
quality) and a security specialist (auth, SSRF, secrets, exposure), because the
service is public. Fixes are re-reviewed by fresh reviewers. At most three
remediation rounds without asking; the owner authorized a 4th on 2026-11-14.

## Review log

- **Round 1** (review of `985028d`, fixed in `2014e0a`). Fixed:
  - malformed links;
  - the URL parser differential;
  - first-hop-only checks, partly; see round 2;
  - the browser running as the login user;
  - unbounded memory;
  - the `ValueError`-to-400 mapping;
  - map search capped before filtering;
  - `form` stripped;
  - the sitemap deadline;
  - byte-bounded status pages;
  - `stealth` crawls without a proxy;
  - the scope after a redirect;
  - `fullPage` and MIME;
  - IPv6;
  - `with_scheme`;
  - the TTL;
  - `enhanced`;
  - browser restart;
  - middleware;
  - the `/mcp` mount;
  - warnings;
  - the robots match;
  - the event loop;
  - `close()`;
  - query URLs;
  - browser slots;
  - tests;
  - the installer;
  - removing no-auth;
  - the deploy target;
  - release deploys.

  Accepted:
  - Firecrawl depth semantics (sitemap pages at depth 1, v1 `maxDepth` as hops; documented);
  - `sudo playwright install-deps` from the venv (the login user already has sudo).
- **Round 2** (re-review of `2014e0a`).
  - Fixed:
    - loopback reachable through redirect hops, popups, workers, websockets and DNS rebinding (kernel `IPAddressDeny` with socket activation);
    - DNS-lookup DoS in the browser guard (literal-only guard);
    - Chromium without a sandbox (`CRAWL4AI_CHROMIUM_SANDBOX`, `ProtectProc`, `PrivateDevices`);
    - regex DoS (`regex` timeouts and a per-crawl budget, failing closed);
    - memory accounting and a global cap;
    - no rollback on a failed health check;
    - discovery not bounded by its deadline;
    - malformed request URLs giving 500;
    - false browser restarts;
    - release pruning;
    - same-host path redirects;
    - `crawl_status` warnings;
    - config messages.
  - Accepted: version-pinned (not hash-pinned) uv installer.
- **Round 3** (re-review of `efd6209`: all ten round-2 claims hold).
  - Fixed:
    - the installer now proves the kernel filter is active (a process in the service's cgroup must be refused on loopback) and rolls back otherwise;
    - private DNS resolvers from `/etc/resolv.conf` are allowed, so other hosts keep resolving names;
    - the crawl scope's shared state is locked, and the sitemap parse thread stops at the deadline;
    - a failed first install stops and disables its units.
  - Accepted: the Chromium sandbox is enabled through Crawl4AI's `CRAWL4AI_CHROMIUM_SANDBOX` variable (verified on the target: no `--no-sandbox`).

## Open after round 3 (re-review of `281c216`; fixed in round 4)

- **Medium. `IPAddressAllow=127.0.0.53` opens every port on that address.** Linux delivers 127.0.0.53 to wildcard-bound sockets (sshd `0.0.0.0:22` here, Docker published ports). This host does not need it (DNS is 1.1.1.1/8.8.8.8). Fix: allow it never; on systemd-resolved hosts, bind `/run/systemd/resolve/resolv.conf` over `/etc/resolv.conf` in the unit.
- **Medium (other hosts).** Resolvers on loopback, link-local or unspecified addresses (`::1`, `127.0.0.1`, `169.254.169.254`) would be auto-allowed. Fix: never auto-allow them; warn instead.
- **Medium (other hosts).** A rerun after a failed first install restores the failed units and runs unfiltered. Fix: delete the unit files on a first-install failure; re-check the filter after any restore.
- **Low.** The filter probe counts any curl failure as "refused" and only tests IPv4 loopback. Fix: positive control outside the cgroup, connect-stage failure only, a temporary dual-stack listener, and test `::1` and `127.0.0.53`.
- **Low.** `CrawlScope.rebase` is not under the lock. The stop-on-timeout path of `sitemap_urls` has no test.

## Round 4 (authorized by the user on 2026-11-14; candidate `2a9d015`)

- **Reproduced before the fix.** Two connections from inside the live service's cgroup:
  - `127.0.0.53:22` connected to sshd;
  - the VM's own public IPv6 (`[…::ba]:22`) returned the sshd banner. This gap was found during the round: the URL guard accepts global addresses, and the deny list did not cover the machine's own ones.
- **Filter behavior.** Blocked connections time out, because the filter drops the SYN; connect() never returns EPERM.
- **Fixed:**
  - `deploy/netcheck.py` builds the unit's network settings:
    - it never allows loopback, link-local, unspecified or own-address resolvers;
    - on systemd-resolved hosts it binds resolved's upstream list over `/etc/resolv.conf`;
    - it denies the machine's own addresses.
  - The probe:
    - uses a dual-stack listener;
    - makes a control connection from outside the cgroup first;
    - counts only a drop, EPERM or EACCES as blocked;
    - tests 127.0.0.1, 127.0.0.53, `::1` and the machine's own addresses.
  - Rollback:
    - restored units must pass the health check and the probe again;
    - otherwise, and on a first install, the units are removed.
  - The read and write of the `CrawlScope` base are now done under the lock.
  - New tests: NET-01..NET-10, a scope race test and a test for the sitemap deadline.
- **Verified on the VPS:**
  - 130 tests pass;
  - 8 mutations were each caught;
  - the deploy probe reported every target blocked;
  - 127.0.0.53 and the machine's own IPv6 time out, while example.com:443 connects;
  - a page served on the machine's own IPv6 answers directly but times out through the API;
  - the key was kept;
  - forced-failure runs showed rollback restoring units that pass, removing units that fail, and no restore on a rerun;
  - `BindReadOnlyPaths` works over a symlinked destination (systemd 257).
- **Re-review** by two fresh reviewers (security, and general):
  - items 1, 2, 4 and 5 of "Open after round 3" are closed;
  - item 3 is closed for every path that reaches `rollback()`.

## Open after round 4 (needs a decision for a 5th round)

- **Medium (other hosts).** Some failures leave enabled units that never passed the probe: a `systemctl restart`/`start` that fails under `set -e`, or an interrupt (SIGINT/SIGHUP) during the health wait. These exit before `rollback()`.
  - Fix: an EXIT trap; `enable` only after the probe passes; rollback steps made non-fatal.
- **Medium (other hosts).** The machine's own addresses are denied one exact address at a time, which misses:
  - local prefix routes (AnyIP);
  - rotating IPv6 temporary addresses;
  - on-link IPv6 neighbours with global addresses (home LAN, AWS dual-stack subnets).

  This VPS has a single `/128` and no local prefix routes, but its address changes (next item).
  - Fix: deny local-route prefixes and on-link IPv6 prefixes.
- **Medium (this host).** The list of own addresses is fixed at install time, but this VM's public IPv6 changed while it was suspended (`…::ba` on 2026-11-14, `…::d6` at the 2026-11-15 deploy). Until the installer ran again, the new address was not denied, so the browser could reach the host's services listening on all addresses through it. The 2026-11-15 run denies `…::d6`; the next change will open the gap again.
  - Fix: deny the address's whole prefix instead of the `/128`, and check the addresses again whenever the service starts.
- **Low.**
  - The probe's listener never accepts: with roughly 60 or more own addresses, the backlog fills up and later connections count as "dropped".
  - A rollback after `--port N` health-checks the new port and removes working previous units.
  - An unreadable `/proc/net/fib_trie` yields no own addresses and no warning.
  - Up to about 90 s of serving before the probe on a host without the filter.
- **Low (tests).**
  - The race test passes if the reader thread crashes.
  - The deadline test pins `urls == []`.
  - Nothing tests the `key=value` output that `install.sh` parses.
  - The probe tests patch out the machine's own addresses and do not assert anything about `::1`.
- **Docs.**
  - README: "continua na release anterior" no longer holds when the previous units fail the probe.
  - The kernel drops connections rather than refusing them.
- **Optional.**
  - Deny NAT64 `64:ff9b::/96` and Azure's `168.63.129.16`.
  - Limit allowed private resolvers to port 53 (nftables). Today this is accepted and documented.

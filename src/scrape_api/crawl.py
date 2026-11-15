"""In-memory crawl jobs: breadth-first, scoped to the start URL, robots.txt respected."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

import regex

from .config import Settings
from .engine import Engine, FetchError, ScrapeParams
from .htmlinfo import normalize_url
from .mapper import Mapper, host_in_scope
from .models import InvalidRequest

log = logging.getLogger(__name__)

PAGE_SIZE = 50
PAGE_BYTES = 10 * 1024 * 1024
DEFAULT_CRAWL_CONCURRENCY = 2
MAX_PATTERNS = 32
MAX_PATTERN_CHARS = 256
MAX_MATCHED_PATH = 2048
# Path patterns come from the caller: bound each match and their total cost per
# crawl, so a catastrophic-backtracking pattern cannot freeze the service.
PATTERN_TIMEOUT_S = 0.05
PATTERN_BUDGET_S = 2.0

Status = Literal["scraping", "completed", "failed", "cancelled"]


class TooManyJobs(Exception):
    pass


def _in_prefix(path: str, base_path: str) -> bool:
    prefix = base_path.rstrip("/")
    return not prefix or path == prefix or path.startswith(prefix + "/")


@dataclass
class CrawlScope:
    base_host: str
    base_path: str
    allow_subdomains: bool = False
    allow_external: bool = False
    entire_domain: bool = False
    include: tuple[regex.Pattern, ...] = ()
    exclude: tuple[regex.Pattern, ...] = ()
    pattern_seconds: float = 0.0
    broken: str | None = None
    # allows() runs on the event loop and in the sitemap-parsing thread.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def build(
        cls,
        start_url: str,
        *,
        include_paths: list[str],
        exclude_paths: list[str],
        allow_subdomains: bool = False,
        allow_external: bool = False,
        entire_domain: bool = False,
    ) -> "CrawlScope":
        patterns = include_paths + exclude_paths
        if len(patterns) > MAX_PATTERNS or any(len(p) > MAX_PATTERN_CHARS for p in patterns):
            raise InvalidRequest(f"at most {MAX_PATTERNS} path patterns of {MAX_PATTERN_CHARS} characters")
        try:
            include = tuple(regex.compile(p) for p in include_paths)
            exclude = tuple(regex.compile(p) for p in exclude_paths)
        except regex.error as exc:
            raise InvalidRequest(f"invalid includePaths/excludePaths regex: {exc}") from exc
        parts = urlsplit(start_url)
        return cls(
            parts.hostname or "",
            parts.path or "/",
            allow_subdomains=allow_subdomains,
            allow_external=allow_external,
            entire_domain=entire_domain,
            include=include,
            exclude=exclude,
        )

    def rebase(self, final_url: str) -> None:
        """Follow a redirect of the start page (site.com/help -> help.site.com/, /docs -> /documentation/)."""
        parts = urlsplit(final_url)
        if not parts.hostname:
            return
        path = parts.path or "/"
        with self._lock:  # allows() may be reading the base in the sitemap thread
            if not host_in_scope(parts.hostname, self.base_host, False) or not _in_prefix(path, self.base_path):
                self.base_host, self.base_path = parts.hostname, path

    def _search(self, pattern: regex.Pattern, path: str) -> bool:
        """Fail closed: a pattern that times out or exhausts the budget matches nothing."""
        if self.broken:
            return False
        started = time.perf_counter()
        try:
            return pattern.search(path, timeout=PATTERN_TIMEOUT_S) is not None
        except TimeoutError:
            with self._lock:
                self.broken = self.broken or f"path pattern {pattern.pattern!r} is too slow; no more links are followed"
            return False
        finally:
            with self._lock:
                self.pattern_seconds += time.perf_counter() - started
                if self.pattern_seconds > PATTERN_BUDGET_S and not self.broken:
                    self.broken = "path patterns used their time budget; no more links are followed"

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        with self._lock:
            base_host, base_path = self.base_host, self.base_path
        same_site = host_in_scope(parts.hostname, base_host, self.allow_subdomains)
        if not same_site and not self.allow_external:
            return False
        path = (parts.path or "/")[:MAX_MATCHED_PATH]
        if same_site and not self.entire_domain and not _in_prefix(path, base_path):
            return False
        if self.broken:
            return False
        if self.include and not any(self._search(p, path) for p in self.include):
            return False
        if any(self._search(p, path) for p in self.exclude) or self.broken:
            return False
        return True


def _doc_bytes(value: Any) -> int:
    """Approximate in-memory size of a document, counting nested lists and dicts."""
    if isinstance(value, str):
        # CPython stores up to 4 bytes per character; ASCII-only text uses 1.
        return len(value) if value.isascii() else len(value) * 4
    if isinstance(value, dict):
        return 64 + sum(_doc_bytes(k) + _doc_bytes(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return 64 + sum(_doc_bytes(v) for v in value)
    return 32


@dataclass
class CrawlJob:
    id: str
    url: str
    limit: int
    created_at: float
    ttl: float
    status: Status = "scraping"
    finished_at: float | None = None
    total: int = 0
    completed: int = 0
    data: list[dict[str, Any]] = field(default_factory=list)
    sizes: list[int] = field(default_factory=list)
    bytes: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    robots_blocked: list[str] = field(default_factory=list)
    error: str | None = None
    warning: str | None = None
    stopped: bool = False  # hit a memory cap: the rest of the queue is drained unfetched
    task: asyncio.Task | None = None

    @property
    def finished(self) -> bool:
        return self.status != "scraping"

    @property
    def expires_at(self) -> float:
        return (self.finished_at or time.time()) + self.ttl

    def finish(self, status: Status) -> None:
        if not self.finished:
            self.status = status
            self.finished_at = time.time()

    def add(self, doc: dict[str, Any]) -> None:
        size = _doc_bytes(doc)
        self.data.append(doc)
        self.sizes.append(size)
        self.bytes += size

    def status_body(self, skip: int, next_base: str) -> dict[str, Any]:
        end, page_bytes = skip, 0
        while end < len(self.data) and end - skip < PAGE_SIZE:
            if end > skip and page_bytes + self.sizes[end] > PAGE_BYTES:
                break
            page_bytes += self.sizes[end]
            end += 1
        body: dict[str, Any] = {
            "success": True,
            "status": self.status,
            "total": self.total,
            "completed": self.completed,
            "creditsUsed": self.completed,
            "expiresAt": datetime.fromtimestamp(self.expires_at, timezone.utc).isoformat(),
            "next": f"{next_base}?skip={end}" if end < len(self.data) else None,
            "data": self.data[skip:end],
        }
        if self.error:
            body["error"] = self.error
        if self.warning:
            body["warning"] = self.warning
        return body


@dataclass
class CrawlRequest:
    url: str
    limit: int
    max_depth: int | None
    scope: CrawlScope
    sitemap: str
    ignore_query: bool
    delay: float
    concurrency: int
    params: ScrapeParams


class CrawlManager:
    def __init__(self, engine: Engine, mapper: Mapper, settings: Settings):
        self.engine = engine
        self.mapper = mapper
        self.settings = settings
        self.jobs: dict[str, CrawlJob] = {}
        # Crawls never take every browser slot, so interactive scrapes keep one.
        self._slots = asyncio.Semaphore(max(1, settings.max_concurrency - 1))

    def total_bytes(self) -> int:
        return sum(job.bytes for job in self.jobs.values())

    def _purge(self) -> None:
        now = time.time()
        for job_id in [j.id for j in self.jobs.values() if j.finished and j.expires_at < now]:
            del self.jobs[job_id]
        finished = sorted((j for j in self.jobs.values() if j.finished), key=lambda j: j.created_at)
        # Evict the oldest finished crawls while over the job count or the memory cap.
        while finished and (
            len(self.jobs) >= self.settings.max_crawl_jobs
            or self.total_bytes() >= self.settings.max_total_crawl_bytes
        ):
            del self.jobs[finished.pop(0).id]
        if len(self.jobs) >= self.settings.max_crawl_jobs:
            raise TooManyJobs(f"{len(self.jobs)} crawls are running; wait for one to finish")
        if self.total_bytes() >= self.settings.max_total_crawl_bytes:
            raise TooManyJobs("running crawls already use the memory reserved for results; wait for one to finish")

    def start(self, request: CrawlRequest) -> CrawlJob:
        self._purge()
        job = CrawlJob(
            id=str(uuid.uuid4()),
            url=request.url,
            limit=request.limit,
            created_at=time.time(),
            ttl=self.settings.crawl_ttl_seconds,
        )
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, request))
        return job

    def get(self, job_id: str) -> CrawlJob | None:
        self._purge_expired_one(job_id)
        return self.jobs.get(job_id)

    def _purge_expired_one(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job and job.finished and job.expires_at < time.time():
            del self.jobs[job_id]

    def cancel(self, job_id: str) -> bool:
        """Cancel a running crawl. Returns False when it had already finished."""
        job = self.jobs[job_id]
        if job.finished:
            return False
        job.finish("cancelled")
        if job.task:
            job.task.cancel()
        return True

    async def close(self) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task and not j.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _over_budget(self, job: CrawlJob) -> str | None:
        if job.bytes >= self.settings.max_crawl_bytes:
            return f"stopped early: results reached {self.settings.max_crawl_bytes // (1024 * 1024)} MB"
        if self.total_bytes() >= self.settings.max_total_crawl_bytes:
            return "stopped early: all crawl results together reached the memory cap"
        return None

    async def _run(self, job: CrawlJob, req: CrawlRequest) -> None:
        queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        seen: set[str] = set()

        def key_of(url: str) -> str | None:
            return normalize_url(url, drop_query=req.ignore_query)

        def enqueue(url: str, depth: int) -> None:
            full, key = normalize_url(url), key_of(url)
            if full is None or key is None or key in seen or job.total >= job.limit:
                return
            seen.add(key)
            job.total += 1
            queue.put_nowait((full, depth))

        async def worker() -> None:
            while True:
                url, depth = await queue.get()
                try:
                    if job.stopped:
                        continue
                    if req.delay:
                        await asyncio.sleep(req.delay)
                    async with self._slots:
                        page = await self.engine.scrape(url, req.params)
                    if depth == 0:
                        req.scope.rebase(page.final_url)
                        final_key = key_of(page.final_url)
                        if final_key:
                            seen.add(final_key)
                    job.add(page.document(req.params.formats))
                    over = self._over_budget(job)
                    if over:
                        job.stopped, job.warning = True, over
                        continue
                    if req.sitemap != "only" and (req.max_depth is None or depth < req.max_depth):
                        for link in page.links:
                            if req.scope.allows(link.url):
                                enqueue(link.url, depth + 1)
                        if req.scope.broken and not job.warning:
                            job.warning = req.scope.broken
                except FetchError as exc:
                    if exc.robots_blocked:
                        job.robots_blocked.append(url)
                    else:
                        job.errors.append(_error_entry(url, str(exc)))
                except ValueError as exc:  # URLNotAllowed, InvalidRequest
                    job.errors.append(_error_entry(url, str(exc)))
                except Exception as exc:  # one bad page must not kill the crawl
                    log.exception("crawl %s: page %s failed", job.id, url)
                    job.errors.append(_error_entry(url, type(exc).__name__))
                finally:
                    job.completed += 1
                    queue.task_done()

        workers: list[asyncio.Task] = []
        try:
            enqueue(req.url, 0)
            # Workers start on the start page while the sitemaps are still being read.
            workers = [asyncio.create_task(worker()) for _ in range(req.concurrency)]
            if req.sitemap != "skip":
                try:
                    seeds = await self.mapper.sitemap_urls(req.url, accept=req.scope.allows, max_urls=job.limit * 2)
                except Exception as exc:
                    log.info("crawl %s: sitemap discovery failed: %s", job.id, exc)
                    seeds = []
                for seed in seeds:
                    enqueue(seed, 1)
            await queue.join()
            job.total = job.completed
            if not job.data and job.errors:
                job.error = job.errors[0]["error"]
                job.finish("failed")
            else:
                job.finish("completed")
        except asyncio.CancelledError:
            job.finish("cancelled")
        except Exception as exc:
            log.exception("crawl %s failed", job.id)
            job.error = type(exc).__name__
            job.finish("failed")
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


def _error_entry(url: str, error: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "url": url,
        "error": error,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

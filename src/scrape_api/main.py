"""FastAPI app: Firecrawl-style REST routes (/v2, /v1), /mcp, bearer-key auth."""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings
from .crawl import TooManyJobs
from .engine import FetchError, NoProxyConfigured
from .mcp_server import build_mcp, mcp_http_app
from .models import CrawlBody, InvalidRequest, MapBody, ScrapeBody
from .service import Services, scrape_params, with_scheme
from .urlguard import URLNotAllowed

log = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/health"})


class BearerAuth:
    """Pure ASGI middleware, so it also guards the MCP app and its streaming responses."""

    def __init__(self, app, keys: tuple[str, ...]):
        self.app = app
        self.keys = [k.encode() for k in keys]

    def _authorized(self, scope) -> bool:
        token = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization" and value[:7].lower() == b"bearer ":
                token = value[7:].strip()
                break
        # Compare against every key so timing does not reveal which one matched.
        matched = False
        for key in self.keys:
            matched |= hmac.compare_digest(token, key)
        return matched

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan" or (scope["type"] == "http" and scope["path"] in PUBLIC_PATHS):
            return await self.app(scope, receive, send)
        if self._authorized(scope):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"success": False, "error": "Unauthorized: missing or invalid API key"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"success": False, "error": message}, status_code=status)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    services = Services(settings)
    mcp = build_mcp(services)
    mcp_app = mcp_http_app(mcp)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await services.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await services.close()

    app = FastAPI(title="scrape-api", version=version("scrape-api"), lifespan=lifespan)
    app.state.services = services
    app.add_middleware(BearerAuth, keys=settings.api_keys)

    def base_url(request: Request) -> str:
        return settings.public_url or str(request.base_url).rstrip("/")

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors())
        return _error(400, f"Bad Request: {details}")

    @app.exception_handler(URLNotAllowed)
    async def _url_not_allowed(_: Request, exc: URLNotAllowed):
        return _error(400, f"URL not allowed: {exc}")

    @app.exception_handler(NoProxyConfigured)
    async def _no_proxy(_: Request, exc: NoProxyConfigured):
        return _error(400, str(exc))

    @app.exception_handler(InvalidRequest)
    async def _invalid(_: Request, exc: InvalidRequest):
        return _error(400, str(exc))

    @app.exception_handler(TooManyJobs)
    async def _too_many(_: Request, exc: TooManyJobs):
        return _error(429, str(exc))

    @app.exception_handler(FetchError)
    async def _fetch_error(_: Request, exc: FetchError):
        return _error(403 if exc.robots_blocked else 502, f"Fetch failed: {exc}")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return _error(500, "Internal error")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v2/scrape")
    @app.post("/v1/scrape")
    async def scrape(body: ScrapeBody):
        params = scrape_params(body)
        page = await services.engine.scrape(with_scheme(body.url), params)
        return {"success": True, "data": page.document(params.formats)}

    @app.post("/v2/map")
    async def map_v2(body: MapBody):
        return {"success": True, "links": await services.map(body)}

    @app.post("/v1/map")
    async def map_v1(body: MapBody):
        return {"success": True, "links": [item["url"] for item in await services.map(body)]}

    def _crawl_routes(version: str) -> None:
        prefix = f"/{version}/crawl"

        @app.post(prefix)
        async def start_crawl(body: CrawlBody, request: Request):
            job = await services.start_crawl(body)
            return {"success": True, "id": job.id, "url": f"{base_url(request)}{prefix}/{job.id}"}

        @app.get(prefix + "/{job_id}")
        async def crawl_status(job_id: str, request: Request, skip: int = 0):
            job = services.crawls.get(job_id)
            if job is None:
                return _error(404, "Crawl job not found or expired")
            return job.status_body(max(0, skip), f"{base_url(request)}{prefix}/{job_id}")

        @app.get(prefix + "/{job_id}/errors")
        async def crawl_errors(job_id: str):
            job = services.crawls.get(job_id)
            if job is None:
                return _error(404, "Crawl job not found or expired")
            return {"errors": job.errors, "robotsBlocked": job.robots_blocked}

        @app.delete(prefix + "/{job_id}")
        async def cancel_crawl(job_id: str):
            if services.crawls.get(job_id) is None:
                return _error(404, "Crawl job not found or expired")
            if not services.crawls.cancel(job_id):
                return _error(409, "Crawl already finished")
            return {"success": True, "status": "cancelled"}

    _crawl_routes("v2")
    _crawl_routes("v1")

    # A plain route (not a catch-all mount), so other paths get FastAPI's JSON 404/405.
    app.add_route("/mcp", mcp_app)
    return app

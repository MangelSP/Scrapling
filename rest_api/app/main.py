import asyncio
import logging
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .config import Settings
from .crawl import JobStore, run_crawl
from .extract import extract_fields, extract_many
from .fetch import FetchError, Fetcher
from .security import RateLimiter, record_result, require_api_key

log = logging.getLogger("scraper-api")  # nunca se registran cuerpos ni claves

Mode = Literal["http", "browser"]


class ScrapeReq(BaseModel):
    url: str
    mode: Mode = "http"
    wait_for: str | None = None
    timeout: float | None = Field(default=None, gt=0, le=120)
    headers: dict[str, str] | None = None


class ExtractReq(ScrapeReq):
    selectors: dict[str, str] = Field(min_length=1, max_length=50)
    many: bool = False


class CrawlReq(BaseModel):
    start_url: str
    max_pages: int = Field(default=10, ge=1)
    same_domain: bool = True
    mode: Mode = "http"
    selectors: dict[str, str] | None = None


def create_app(settings: Settings | None = None, fetcher: Fetcher | None = None, redis=None) -> FastAPI:
    s = settings or Settings.from_env()
    app = FastAPI(
        title="Scrapling REST API", version="0.1.0",
        docs_url="/docs" if s.enable_docs else None,
        redoc_url=None, openapi_url="/openapi.json" if s.enable_docs else None,
    )
    app.state.settings = s
    if redis is None and s.redis_url:
        from redis.asyncio import from_url  # import perezoso: solo si REDIS_URL está definido
        redis = from_url(s.redis_url, decode_responses=True)
    app.state.redis = redis
    app.state.limiter = RateLimiter(s.rate_limit_per_min)
    app.state.fetcher = fetcher or Fetcher(s)
    app.state.jobs = JobStore(s.max_jobs)
    app.state.tasks = set()

    @app.middleware("http")
    async def usage(request: Request, call_next):
        resp = await call_next(request)
        who = getattr(request.state, "system", None)
        if who and app.state.redis is not None:
            await record_result(app.state.redis, who, resp.status_code,
                                int(resp.headers.get("content-length") or 0))
        return resp

    def _err(e: FetchError):
        log.info("fetch error code=%s", e.code)
        return HTTPException(e.code, str(e))

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/scrape")
    async def scrape(req: ScrapeReq, who: str = Depends(require_api_key)):
        try:
            p = await app.state.fetcher.fetch(req.url, req.mode, req.wait_for, req.timeout, req.headers)
        except FetchError as e:
            raise _err(e)
        log.info("scrape system=%s mode=%s status=%s", who, req.mode, p.status)
        return {"status": p.status, "url_final": p.url_final, "html": p.html,
                "text": p.selector().get_all_text(strip=True)}

    @app.post("/v1/extract")
    async def extract(req: ExtractReq, who: str = Depends(require_api_key)):
        try:
            p = await app.state.fetcher.fetch(req.url, req.mode, req.wait_for, req.timeout, req.headers)
        except FetchError as e:
            raise _err(e)
        sel = p.selector()
        data = extract_many(sel, req.selectors) if req.many else extract_fields(sel, req.selectors)
        log.info("extract system=%s mode=%s status=%s", who, req.mode, p.status)
        return {"status": p.status, "url_final": p.url_final, "data": data}

    @app.post("/v1/crawl", status_code=202)
    async def crawl(req: CrawlReq, who: str = Depends(require_api_key)):
        try:
            app.state.fetcher._check(req.start_url)
        except FetchError as e:
            raise _err(e)
        max_pages = min(req.max_pages, s.max_crawl_pages_cap)
        job = app.state.jobs.create(who, req.start_url, max_pages)
        t = asyncio.create_task(run_crawl(job, app.state.fetcher, req.mode, req.same_domain, req.selectors))
        app.state.tasks.add(t)
        t.add_done_callback(app.state.tasks.discard)
        return {"job_id": job["id"], "status": job["status"], "max_pages": max_pages}

    @app.get("/v1/jobs/{job_id}")
    async def job(job_id: str, who: str = Depends(require_api_key)):
        j = app.state.jobs.jobs.get(job_id)
        if not j or j["owner"] != who:  # cada sistema solo ve sus jobs
            raise HTTPException(404, "Job no encontrado")
        return {k: v for k, v in j.items() if k != "owner"}

    return app


def create_default() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    return create_app()

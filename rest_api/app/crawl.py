"""Crawl asíncrono BFS en memoria. Solo guarda URL/estado/datos extraídos, nunca HTML."""
import asyncio
import uuid
from collections import OrderedDict, deque
from urllib.parse import urldefrag, urljoin, urlsplit

from .extract import extract_fields
from .fetch import FetchError


class JobStore:
    def __init__(self, max_jobs: int):
        self.max_jobs = max_jobs
        self.jobs: "OrderedDict[str, dict]" = OrderedDict()

    def create(self, owner: str, start_url: str, max_pages: int) -> dict:
        job = {"id": uuid.uuid4().hex, "owner": owner, "status": "running", "start_url": start_url,
               "max_pages": max_pages, "pages_done": 0, "results": [], "error": None}
        self.jobs[job["id"]] = job
        while len(self.jobs) > self.max_jobs:
            self.jobs.popitem(last=False)
        return job


async def run_crawl(job: dict, fetcher, mode: str, same_domain: bool, selectors: dict | None, concurrency: int = 2):
    host0 = urlsplit(job["start_url"]).hostname
    seen = {urldefrag(job["start_url"])[0]}
    queue = deque([job["start_url"]])
    try:
        while queue and job["pages_done"] < job["max_pages"]:
            url = queue.popleft()
            job["pages_done"] += 1  # cuenta intentos: tope duro
            try:
                page = await fetcher.fetch(url, mode=mode)
            except FetchError as e:
                job["results"].append({"url": url, "error": str(e)})
                continue
            sel = page.selector()
            item = {"url": page.url_final, "status": page.status}
            if selectors:
                item["data"] = extract_fields(sel, selectors)
            job["results"].append(item)
            for href in sel.css("a::attr(href)").getall():
                link = urldefrag(urljoin(page.url_final, str(href)))[0]
                p = urlsplit(link)
                if p.scheme not in ("http", "https") or link in seen:
                    continue
                if same_domain and p.hostname != host0:
                    continue
                seen.add(link)
                queue.append(link)
        job["status"] = "done"
    except Exception as e:
        job["status"] = "failed"
        job["error"] = type(e).__name__

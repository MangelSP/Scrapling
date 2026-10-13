"""Obtención de páginas con Scrapling (modo http / browser) con SSRF en cada salto."""
import asyncio
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from scrapling.parser import Selector

from .config import Settings
from .security import SSRFError, validate_url


class FetchError(Exception):
    def __init__(self, msg: str, code: int = 502):
        super().__init__(msg)
        self.code = code


@dataclass
class Page:
    status: int
    url_final: str
    body: bytes
    encoding: str = "utf-8"

    @property
    def html(self) -> str:
        return self.body.decode(self.encoding or "utf-8", errors="replace")

    def selector(self) -> Selector:
        return Selector(self.html, url=self.url_final)


# --- Backends (se sustituyen en tests) ---
def _http_get(url: str, timeout: float, headers: dict | None):
    from scrapling.fetchers import Fetcher
    r = Fetcher.get(url, timeout=timeout, headers=headers or {}, follow_redirects=False, retries=1)
    loc = r.headers.get("location") or r.headers.get("Location")
    return r.status, bytes(r.body), loc, getattr(r, "encoding", "utf-8")


def _browser_get(url: str, timeout: float, headers: dict | None, wait_for: str | None):
    from scrapling.fetchers import DynamicFetcher
    kw = dict(timeout=int(timeout * 1000), network_idle=True, disable_resources=True)
    if headers:
        kw["extra_headers"] = headers
    if wait_for:
        kw["wait_selector"] = wait_for
    r = DynamicFetcher.fetch(url, **kw)
    return r.status, bytes(r.body), str(r.url), getattr(r, "encoding", "utf-8")


class Fetcher:
    def __init__(self, settings: Settings, http_backend=_http_get, browser_backend=_browser_get, resolver=None):
        self.s = settings
        self.http_backend = http_backend
        self.browser_backend = browser_backend
        self.browser_sem = asyncio.Semaphore(settings.max_browsers)
        self.resolver = resolver
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _check(self, url: str) -> None:
        try:
            if self.resolver:
                validate_url(url, self.resolver)
            else:
                validate_url(url)
        except SSRFError as e:
            raise FetchError(str(e), 400)

    def _limit(self, body: bytes) -> None:
        if len(body) > self.s.max_response_mb * 1024 * 1024:
            raise FetchError("Respuesta excede MAX_RESPONSE_MB", 413)

    async def _allowed_by_robots(self, url: str) -> bool:
        if not self.s.respect_robots:
            return True
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                robots_url = origin + "/robots.txt"
                self._check(robots_url)
                status, body, _, _ = await asyncio.to_thread(
                    self.http_backend, robots_url, min(10, self.s.request_timeout_s), None)
                if status == 200:
                    rp.parse(body.decode("utf-8", "replace").splitlines())
                    self._robots[origin] = rp
                else:
                    self._robots[origin] = None  # sin robots -> permitido
            except Exception:
                self._robots[origin] = None
        rp = self._robots[origin]
        return rp is None or rp.can_fetch("*", url)

    async def fetch(self, url: str, mode: str = "http", wait_for: str | None = None,
                    timeout: float | None = None, headers: dict | None = None) -> Page:
        timeout = min(timeout or self.s.request_timeout_s, self.s.request_timeout_s)
        self._check(url)
        if not await self._allowed_by_robots(url):
            raise FetchError("Bloqueado por robots.txt", 403)
        try:
            if mode == "browser":
                return await asyncio.wait_for(self._browser(url, timeout, headers, wait_for), timeout + 5)
            return await asyncio.wait_for(self._http(url, timeout, headers), timeout * (self.s.max_redirects + 1))
        except FetchError:
            raise
        except asyncio.TimeoutError:
            raise FetchError("Timeout", 504)
        except Exception as e:  # no se filtra contenido, solo el tipo
            raise FetchError(f"Error al obtener la página ({type(e).__name__})", 502)

    async def _http(self, url, timeout, headers) -> Page:
        current = url
        for _ in range(self.s.max_redirects + 1):
            self._check(current)  # cada salto se revalida
            status, body, loc, enc = await asyncio.to_thread(self.http_backend, current, timeout, headers)
            if status in (301, 302, 303, 307, 308) and loc:
                current = urljoin(current, loc)
                continue
            self._limit(body)
            return Page(status, current, body, enc)
        raise FetchError("Demasiadas redirecciones", 502)

    async def _browser(self, url, timeout, headers, wait_for) -> Page:
        async with self.browser_sem:
            status, body, final, enc = await asyncio.to_thread(
                self.browser_backend, url, timeout, headers, wait_for)
        self._check(final)  # verificación posterior de la URL final
        self._limit(body)
        return Page(status, final, body, enc)

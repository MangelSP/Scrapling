import asyncio
import socket

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, parse_api_keys
from app.fetch import Fetcher
from app.main import create_app
from app.security import SSRFError, validate_url

KEY = "k-test-123"
H = {"X-API-Key": KEY}

HTML = """<html><head><title>T</title></head><body>
<h1 class="t">Hola</h1>
<ul><li class="p"><span class="n">A</span><span class="v">1</span></li>
<li class="p"><span class="n">B</span><span class="v">2</span></li></ul>
<a href="/p2">p2</a><a href="https://otro.example/x">ext</a></body></html>"""

PUBLIC = {"example.test": ["93.184.216.34"], "evil.test": ["10.0.0.5"], "otro.example": ["93.184.216.35"]}


def resolver(host, port):
    if host not in PUBLIC:
        raise socket.gaierror
    return PUBLIC[host]


def make(pages=None, **kw):
    """pages: url -> (status, body, location)"""
    pages = pages if pages is not None else {}
    calls = []

    def backend(url, timeout, headers):
        calls.append(url)
        st, body, loc = pages.get(url, (200, HTML, None))
        return st, body.encode(), loc, "utf-8"

    s = Settings(api_keys={"pos": KEY}, **kw)
    app = create_app(s, Fetcher(s, http_backend=backend, resolver=resolver))
    return TestClient(app), calls


# ---- auth ----
def test_health_sin_auth():
    c, _ = make()
    assert c.get("/health").json() == {"status": "ok"}


def test_auth_falta_incorrecta_correcta():
    c, _ = make()
    body = {"url": "http://example.test/"}
    assert c.post("/v1/scrape", json=body).status_code == 401
    assert c.post("/v1/scrape", json=body, headers={"X-API-Key": "mala"}).status_code == 401
    r = c.post("/v1/scrape", json=body, headers=H)
    assert r.status_code == 200 and r.json()["status"] == 200 and "Hola" in r.json()["text"]


def test_sin_claves_configuradas_503():
    s = Settings(api_keys={})
    c = TestClient(create_app(s))
    assert c.post("/v1/scrape", json={"url": "http://example.test/"}, headers=H).status_code == 503


def test_parse_keys():
    assert parse_api_keys("pos:a, n8n:b:c ,x") == {"pos": "a", "n8n": "b:c"}


def test_docs_desactivadas_por_defecto():
    c, _ = make()
    assert c.get("/docs").status_code == 404
    s = Settings(api_keys={"pos": KEY}, enable_docs=True)
    assert TestClient(create_app(s)).get("/docs").status_code == 200


# ---- rate limit ----
def test_rate_limit_por_clave():
    c, _ = make(rate_limit_per_min=3)
    b = {"url": "http://example.test/"}
    assert [c.post("/v1/scrape", json=b, headers=H).status_code for _ in range(4)] == [200, 200, 200, 429]


# ---- SSRF ----
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://10.1.2.3/", "http://169.254.169.254/latest/meta-data/",
    "file:///etc/passwd", "http://[::1]/", "http://evil.test/", "http://example.test:8080/",
    "ftp://example.test/", "http://user:pw@example.test/", "http://192.168.1.1/",
])
def test_ssrf_bloqueado(url):
    c, calls = make()
    r = c.post("/v1/scrape", json={"url": url}, headers=H)
    assert r.status_code == 400 and calls == []


def test_ssrf_redireccion_a_ip_privada():
    c, calls = make({"http://example.test/": (302, "", "http://169.254.169.254/latest/")})
    r = c.post("/v1/scrape", json={"url": "http://example.test/"}, headers=H)
    assert r.status_code == 400
    assert calls == ["http://example.test/"]  # nunca se pidió el destino interno


def test_ssrf_redireccion_relativa_publica_ok():
    c, _ = make({"http://example.test/": (301, "", "/final")})
    r = c.post("/v1/scrape", json={"url": "http://example.test/"}, headers=H)
    assert r.json()["url_final"] == "http://example.test/final"


def test_ssrf_dns_mixto():
    with pytest.raises(SSRFError):
        validate_url("http://x.test/", lambda h, p: ["93.184.216.34", "127.0.0.1"])


def test_tope_tamano():
    c, _ = make({"http://example.test/": (200, "x" * 2_000_000, None)}, max_response_mb=1)
    assert c.post("/v1/scrape", json={"url": "http://example.test/"}, headers=H).status_code == 413


def test_robots():
    pages = {"http://example.test/robots.txt": (200, "User-agent: *\nDisallow: /priv", None)}
    c, _ = make(pages, respect_robots=True)
    assert c.post("/v1/scrape", json={"url": "http://example.test/priv"}, headers=H).status_code == 403
    assert c.post("/v1/scrape", json={"url": "http://example.test/ok"}, headers=H).status_code == 200
    c2, _ = make(pages, respect_robots=False)
    assert c2.post("/v1/scrape", json={"url": "http://example.test/priv"}, headers=H).status_code == 200


# ---- extracción ----
def test_extract_uno_css_y_xpath():
    c, _ = make()
    r = c.post("/v1/extract", headers=H, json={"url": "http://example.test/", "selectors": {
        "titulo": "h1.t::text", "primero": "li.p .n", "href": "a::attr(href)", "xp": "//span[@class='v']/text()",
        "nada": ".no-existe"}})
    assert r.json()["data"] == {"titulo": "Hola", "primero": "A", "href": "/p2", "xp": "1", "nada": None}


def test_extract_many():
    c, _ = make()
    r = c.post("/v1/extract", headers=H, json={"url": "http://example.test/", "many": True,
                                               "selectors": {"n": "li.p .n", "v": "li.p .v::text"}})
    assert r.json()["data"] == [{"n": "A", "v": "1"}, {"n": "B", "v": "2"}]


# ---- browser: semáforo y SSRF posterior ----
def test_browser_semaforo_y_url_final():
    active = {"n": 0, "max": 0}
    import threading
    lock = threading.Lock()

    def browser(url, timeout, headers, wait_for):
        import time
        with lock:
            active["n"] += 1; active["max"] = max(active["max"], active["n"])
        time.sleep(0.05)
        with lock:
            active["n"] -= 1
        return 200, HTML.encode(), url if "ok" in url else "http://10.0.0.1/", "utf-8"

    s = Settings(api_keys={"pos": KEY}, max_browsers=2)
    f = Fetcher(s, browser_backend=browser, resolver=resolver)

    async def go():
        res = await asyncio.gather(*[f.fetch("http://example.test/ok", "browser") for _ in range(6)])
        return res

    asyncio.run(go())
    assert active["max"] <= 2

    c = TestClient(create_app(s, f))
    r = c.post("/v1/scrape", json={"url": "http://example.test/redir", "mode": "browser"}, headers=H)
    assert r.status_code == 400


# ---- crawl ----
def _wait(c, jid):
    import time
    for _ in range(100):
        j = c.get(f"/v1/jobs/{jid}", headers=H).json()
        if j["status"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("job no terminó")


def test_crawl_tope_duro_y_mismo_dominio():
    def link_page(n):
        return "<html><body>" + "".join(f'<a href="/p{i}">x</a>' for i in range(n, n + 50)) + \
            '<a href="http://otro.example/z">e</a><h1>t</h1></body></html>'
    pages = {f"http://example.test/p{i}": (200, link_page(i), None) for i in range(0, 400)}
    pages["http://example.test/"] = (200, link_page(0), None)
    c, calls = make(pages, max_crawl_pages_cap=5)
    with c:  # mantiene vivo el event loop para la tarea en segundo plano
        r = c.post("/v1/crawl", headers=H, json={"start_url": "http://example.test/", "max_pages": 1000,
                                                 "selectors": {"t": "h1"}})
        assert r.status_code == 202 and r.json()["max_pages"] == 5
        j = _wait(c, r.json()["job_id"])
    assert j["status"] == "done" and len(j["results"]) == 5 and len(calls) == 5
    assert all("example.test" in x["url"] for x in j["results"])
    assert j["results"][0]["data"] == {"t": "t"} and "html" not in j["results"][0]


def test_crawl_ssrf_y_aislamiento_jobs():
    c, _ = make()
    assert c.post("/v1/crawl", headers=H, json={"start_url": "http://127.0.0.1/", "max_pages": 2}).status_code == 400
    assert c.get("/v1/jobs/nope", headers=H).status_code == 404
    assert c.get("/v1/jobs/nope").status_code == 401

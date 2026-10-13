import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings, parse_origins
from app.fetch import Fetcher
from app.main import create_app

from .test_api import HTML, resolver
from .test_redis_keys import FakeRedis

ROOT = Path(__file__).resolve().parent.parent
DOCS_ORIGIN = "https://docs.example.test"
PREFLIGHT = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-api-key,content-type"}


def make(redis=None, **kw):
    s = Settings(**kw)
    f = Fetcher(s, http_backend=lambda u, t, h: (200, HTML.encode(), None, "utf-8"), resolver=resolver)
    return TestClient(create_app(s, f, redis=redis))


def test_openapi_json_versionado_esta_al_dia():
    sys.path.insert(0, str(ROOT))
    from scripts.export_openapi import render
    assert (ROOT / "openapi.json").read_text(encoding="utf-8") == render(), \
        "openapi.json desactualizado: ejecuta `python scripts/export_openapi.py`"


def test_openapi_tiene_security_scheme_y_5_operaciones():
    spec = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    schemes = spec["components"]["securitySchemes"]
    assert any(v["type"] == "apiKey" and v["in"] == "header" and v["name"] == "X-API-Key" for v in schemes.values())
    ops = [(p, m) for p, item in spec["paths"].items() for m in item]
    assert sorted(ops) == sorted([("/health", "get"), ("/v1/scrape", "post"), ("/v1/extract", "post"),
                                  ("/v1/crawl", "post"), ("/v1/jobs/{job_id}", "get")])
    assert "security" in spec["paths"]["/v1/scrape"]["post"]
    assert "security" not in spec["paths"]["/health"]["get"]


def test_sin_cors_por_defecto():
    c = make(api_keys={"pos": "k"})
    r = c.options("/v1/scrape", headers={"Origin": DOCS_ORIGIN, **PREFLIGHT})
    assert "access-control-allow-origin" not in r.headers


def test_preflight_origen_permitido_sin_clave_y_sin_contar_uso():
    redis = FakeRedis()
    redis.add_key("sk")
    c = make(redis, cors_origins=[DOCS_ORIGIN])
    r = c.options("/v1/scrape", headers={"Origin": DOCS_ORIGIN, **PREFLIGHT})
    assert r.status_code == 200
    h = r.headers
    assert h["access-control-allow-origin"] == DOCS_ORIGIN
    assert "POST" in h["access-control-allow-methods"]
    assert "x-api-key" in h["access-control-allow-headers"].lower()
    assert h["access-control-max-age"] == "600"
    assert "access-control-allow-credentials" not in h
    assert redis.hashes == {} and redis.kv.get("scraper:rl:pos") is None
    assert not [k for k in redis.kv if k.startswith("scraper:rl")]


def test_preflight_origen_no_permitido():
    c = make(cors_origins=[DOCS_ORIGIN])
    r = c.options("/v1/scrape", headers={"Origin": "https://evil.example", **PREFLIGHT})
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


def test_respuesta_real_expone_retry_after_solo_a_origen_permitido():
    c = make(cors_origins=[DOCS_ORIGIN], api_keys={"pos": "k"})
    ok = c.post("/v1/scrape", json={"url": "http://example.test/"}, headers={"X-API-Key": "k", "Origin": DOCS_ORIGIN})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == DOCS_ORIGIN
    assert "Retry-After" in ok.headers.get("access-control-expose-headers", "")
    bad = c.post("/v1/scrape", json={"url": "http://example.test/"}, headers={"X-API-Key": "k", "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_parse_origins():
    assert parse_origins(" https://a.com/ ,http://localhost:8080,*,") == ["https://a.com", "http://localhost:8080"]


def test_docs_apagados_por_defecto_y_activables():
    c = make(api_keys={"pos": "k"})
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
    c = make(enable_docs=True)
    assert c.get("/docs").status_code == 200 and c.get("/openapi.json").status_code == 200


def test_from_env_enable_docs_false_y_cors(monkeypatch):
    monkeypatch.setenv("ENABLE_DOCS", "false")
    monkeypatch.setenv("CORS_ORIGINS", DOCS_ORIGIN)
    s = Settings.from_env()
    assert s.enable_docs is False and s.cors_origins == [DOCS_ORIGIN]
    c = TestClient(create_app(s))
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404

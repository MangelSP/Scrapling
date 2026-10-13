import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.fetch import Fetcher
from app.main import create_app
from app.security import key_hash

from .test_api import HTML, resolver

BODY = {"url": "http://example.test/"}


class FakeRedis:
    """Mínimo async: solo lo que usa la API (get, incr, expire, hincrby)."""

    def __init__(self):
        self.kv, self.hashes, self.ttl = {}, {}, {}

    async def get(self, k):
        return self.kv.get(k)

    async def incr(self, k):
        self.kv[k] = int(self.kv.get(k, 0)) + 1
        return self.kv[k]

    async def expire(self, k, s):
        self.ttl[k] = s

    async def hincrby(self, k, f, n):
        h = self.hashes.setdefault(k, {})
        h[f] = h.get(f, 0) + n
        return h[f]

    def add_key(self, key, **rec):
        rec = {"system": "pos", "rate_per_min": 60, "daily_quota": 0, "enabled": True, **rec}
        self.kv[f"scraper:keys:{key_hash(key)}"] = json.dumps(rec)

    def usage(self):
        return next(iter(self.hashes.values()), {})


def make(redis, env_keys=None):
    s = Settings(api_keys=env_keys or {})
    f = Fetcher(s, http_backend=lambda u, t, h: (200, HTML.encode(), None, "utf-8"), resolver=resolver)
    return TestClient(create_app(s, f, redis=redis))


def post(c, key):
    return c.post("/v1/scrape", json=BODY, headers={"X-API-Key": key})


def test_clave_en_redis_ok_y_cuenta_uso():
    r = FakeRedis()
    r.add_key("sk_abc")
    c = make(r)
    assert post(c, "sk_abc").status_code == 200
    u = r.usage()
    assert u["requests"] == 1 and u["bytes"] > 0 and "errors" not in u
    assert all(k.startswith("scraper:usage:pos:") for k in r.hashes)
    assert list(r.ttl.values()).count(90 * 24 * 3600) == 1


def test_clave_revocada_o_desconocida_401():
    r = FakeRedis()
    r.add_key("sk_off", enabled=False)
    c = make(r)
    assert post(c, "sk_off").status_code == 401
    assert post(c, "sk_nope").status_code == 401


def test_cuota_diaria_429():
    r = FakeRedis()
    r.add_key("sk_q", daily_quota=2)
    c = make(r)
    assert post(c, "sk_q").status_code == 200
    assert post(c, "sk_q").status_code == 200
    assert post(c, "sk_q").status_code == 429
    assert r.usage()["requests"] == 2


def test_rate_limit_por_minuto_en_redis_429():
    r = FakeRedis()
    r.add_key("sk_r", rate_per_min=1)
    c = make(r)
    assert post(c, "sk_r").status_code == 200
    assert post(c, "sk_r").status_code == 429


def test_fallback_a_claves_de_entorno():
    r = FakeRedis()
    c = make(r, {"legacy": "env-key"})
    assert post(c, "env-key").status_code == 200
    assert any(k.startswith("scraper:usage:legacy:") for k in r.hashes)
    assert post(c, "otra").status_code == 401


def test_errores_se_cuentan():
    r = FakeRedis()
    r.add_key("sk_e")
    c = make(r)
    res = c.post("/v1/scrape", json={"url": "http://evil.test/"}, headers={"X-API-Key": "sk_e"})
    assert res.status_code >= 400 and r.usage()["errors"] == 1

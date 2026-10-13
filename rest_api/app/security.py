"""Autenticación, rate limit y protección SSRF."""
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from datetime import datetime, timezone

from fastapi import Header, HTTPException, Request

log = logging.getLogger("scraper-api")
USAGE_TTL_S = 90 * 24 * 3600

ALLOWED_PORTS = {80, 443}
METADATA_IPS = {"169.254.169.254", "fd00:ec2::254", "100.100.100.200"}


class SSRFError(ValueError):
    pass


# ---------- Auth ----------
def authenticate(provided: str | None, keys: dict[str, str]) -> str | None:
    """Devuelve el nombre del sistema o None. Compara TODAS las claves en tiempo constante."""
    if not provided:
        return None
    found = None
    for name, key in keys.items():
        if hmac.compare_digest(provided.encode(), key.encode()):
            found = name
    return found


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


async def _lookup_redis(r, provided: str) -> dict | None:
    """Registro de la clave en Redis (`scraper:keys:<sha256>`) o None si no existe."""
    raw = await r.get(f"scraper:keys:{key_hash(provided)}")
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and rec.get("system") else None


async def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> str:
    st = request.app.state
    r = getattr(st, "redis", None)
    if not st.settings.api_keys and r is None:
        raise HTTPException(503, "Servidor sin SCRAPER_API_KEYS configuradas")
    if not x_api_key:
        raise HTTPException(401, "API key ausente o inválida")

    name, rate, quota, redis_down = None, st.settings.rate_limit_per_min, None, False
    if r is not None:
        try:
            rec = await _lookup_redis(r, x_api_key)
        except Exception:
            log.warning("redis no disponible al validar clave")
            rec, redis_down = None, True
        if rec is not None:
            if not rec.get("enabled", True):
                raise HTTPException(401, "API key ausente o inválida")
            name = str(rec["system"])
            rate = int(rec.get("rate_per_min") or rate)
            quota = int(rec["daily_quota"]) if rec.get("daily_quota") else None
    if name is None:
        name = authenticate(x_api_key, st.settings.api_keys)  # respaldo: SCRAPER_API_KEYS
        if name is None:
            if redis_down:
                raise HTTPException(503, "Almacén de claves no disponible")
            raise HTTPException(401, "API key ausente o inválida")

    request.state.system = name
    if r is not None and not redis_down:
        try:
            await _redis_limits(r, name, rate, quota)
            return name
        except HTTPException:
            raise
        except Exception:
            log.warning("redis no disponible para límites; uso memoria")
    if not st.limiter.allow(name):
        raise HTTPException(429, "Límite de peticiones excedido", headers={"Retry-After": "60"})
    return name


async def _redis_limits(r, name: str, rate: int, quota: int | None) -> None:
    """Ventana fija por sistema y minuto + cuota diaria; cuenta la petición en el hash de uso."""
    window = int(time.time() // 60)
    rl = f"scraper:rl:{name}:{window}"
    n = await r.incr(rl)
    if n == 1:
        await r.expire(rl, 90)
    if n > rate:
        raise HTTPException(429, "Límite de peticiones excedido", headers={"Retry-After": "60"})
    usage = f"scraper:usage:{name}:{_today()}"
    total = await r.hincrby(usage, "requests", 1)
    await r.expire(usage, USAGE_TTL_S)
    if quota is not None and total > quota:
        await r.hincrby(usage, "requests", -1)
        raise HTTPException(429, "Cuota diaria excedida", headers={"Retry-After": "3600"})


async def record_result(r, name: str, status: int, size: int) -> None:
    """Errores y bytes del día para el sistema (no debe romper la respuesta)."""
    try:
        usage = f"scraper:usage:{name}:{_today()}"
        if status >= 400:
            await r.hincrby(usage, "errors", 1)
        if size:
            await r.hincrby(usage, "bytes", size)
        await r.expire(usage, USAGE_TTL_S)
    except Exception:
        log.warning("no se pudo registrar uso")


# ---------- Rate limit (ventana deslizante en memoria, por clave) ----------
class RateLimiter:
    def __init__(self, per_min: int):
        self.per_min = per_min
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, name: str) -> bool:
        now = time.monotonic()
        q = self._hits[name]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_min:
            return False
        q.append(now)
        return True


# ---------- SSRF ----------
def _ip_forbidden(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        str(ip) in METADATA_IPS
        or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
        or not ip.is_global
    )


def resolve_host(host: str, port: int) -> list[str]:
    return [i[4][0] for i in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


def validate_url(url: str, resolver=resolve_host) -> str:
    """Valida esquema, puerto y que TODAS las IPs resueltas sean públicas. Lanza SSRFError."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SSRFError("Solo se permiten esquemas http/https")
    if parts.username or parts.password:
        raise SSRFError("URLs con credenciales no permitidas")
    host = parts.hostname
    if not host:
        raise SSRFError("URL sin host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise SSRFError("Puerto inválido")
    if port not in ALLOWED_PORTS:
        raise SSRFError("Puerto no permitido (solo 80/443)")
    try:
        ips = [host] if _is_ip(host) else resolver(host, port)
    except socket.gaierror:
        raise SSRFError("No se pudo resolver el host")
    if not ips:
        raise SSRFError("No se pudo resolver el host")
    for ip in ips:
        if _ip_forbidden(ipaddress.ip_address(ip.split("%")[0])):
            raise SSRFError("Destino no permitido (IP privada/interna)")
    return url


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False

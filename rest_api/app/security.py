"""Autenticación, rate limit y protección SSRF."""
import hmac
import ipaddress
import socket
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

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


async def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> str:
    st = request.app.state
    if not st.settings.api_keys:
        raise HTTPException(503, "Servidor sin SCRAPER_API_KEYS configuradas")
    name = authenticate(x_api_key, st.settings.api_keys)
    if name is None:
        raise HTTPException(401, "API key ausente o inválida")
    if not st.limiter.allow(name):
        raise HTTPException(429, "Límite de peticiones excedido", headers={"Retry-After": "60"})
    return name


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

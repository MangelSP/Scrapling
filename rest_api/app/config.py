"""Configuración leída del entorno. Nunca se imprimen las claves."""
import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def parse_api_keys(raw: str) -> dict[str, str]:
    """'pos:abc,n8n:def' -> {'pos': 'abc', 'n8n': 'def'} (nombre -> clave)."""
    keys: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        name, key = item.split(":", 1)
        if name.strip() and key.strip():
            keys[name.strip()] = key.strip()
    return keys


@dataclass
class Settings:
    api_keys: dict[str, str] = field(default_factory=dict)
    rate_limit_per_min: int = 60
    max_browsers: int = 2
    max_response_mb: float = 5.0
    request_timeout_s: float = 30.0
    respect_robots: bool = False
    enable_docs: bool = False
    max_redirects: int = 5
    max_crawl_pages_cap: int = 50  # tope duro absoluto de /v1/crawl
    max_jobs: int = 100
    redis_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_keys=parse_api_keys(os.getenv("SCRAPER_API_KEYS", "")),
            rate_limit_per_min=int(os.getenv("RATE_LIMIT_PER_MIN", "60")),
            max_browsers=int(os.getenv("MAX_BROWSERS", "2")),
            max_response_mb=float(os.getenv("MAX_RESPONSE_MB", "5")),
            request_timeout_s=float(os.getenv("REQUEST_TIMEOUT_S", "30")),
            respect_robots=_bool("RESPECT_ROBOTS", False),
            enable_docs=_bool("ENABLE_DOCS", False),
            redis_url=os.getenv("REDIS_URL") or None,
            max_crawl_pages_cap=int(os.getenv("MAX_CRAWL_PAGES", "50")),
        )

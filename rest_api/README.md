# Scrapling REST API

API REST (FastAPI) sobre Scrapling, aislada en `rest_api/` (no modifica el paquete original).

## Endpoints
Todos menos `/health` requieren cabecera `X-API-Key`.

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Estado (sin auth) |
| POST | `/v1/scrape` | `{url, mode: http\|browser, wait_for?, timeout?, headers?}` -> `{status, url_final, html, text}` |
| POST | `/v1/extract` | `{url, mode, selectors: {campo: "css o xpath"}, many?}` -> `{status, url_final, data}` |
| POST | `/v1/crawl` | `{start_url, max_pages, same_domain, mode?, selectors?}` -> `202 {job_id}` |
| GET | `/v1/jobs/{id}` | Estado y resultados del crawl (solo del sistema que lo creó) |

Selectores: CSS por defecto (`h1::text`, `a::attr(href)`); XPath si empieza con `/`, `./`, `(` o `xpath:`.

```bash
curl -X POST http://localhost:8000/v1/extract -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"url":"https://example.com","mode":"http","selectors":{"titulo":"h1::text"}}'
```

## Variables de entorno
`SCRAPER_API_KEYS` (`pos:clave1,n8n:clave2`), `RATE_LIMIT_PER_MIN` (60), `MAX_BROWSERS` (2), `MAX_RESPONSE_MB` (5),
`REQUEST_TIMEOUT_S` (30), `RESPECT_ROBOTS` (false), `ENABLE_DOCS` (false; si true expone `/docs`, sin auth),
`MAX_CRAWL_PAGES` (50, tope duro del crawl).

## Ejecutar
```bash
pip install -e ".[fetchers]" -r rest_api/requirements.txt && playwright install chromium
cd rest_api && SCRAPER_API_KEYS=pos:cambiame uvicorn app.main:create_default --factory --port 8000
# Docker (contexto = raíz del repo)
docker build -f rest_api/Dockerfile.api -t scrapling-api . && docker run -p 8000:8000 -e SCRAPER_API_KEYS=pos:cambiame scrapling-api
```
Tests: `pip install -r rest_api/requirements-dev.txt && cd rest_api && pytest` (sin internet).

## Seguridad y límites
- SSRF: solo http/https en puertos 80/443; se resuelve el host y se rechazan IPs no públicas (loopback, privadas, link-local, metadata). En modo http cada redirección se revalida. En modo browser se valida la URL inicial y la final, pero NO las peticiones intermedias/subrecursos del navegador. Hay una ventana TOCTOU de DNS rebinding (se valida y luego la librería resuelve de nuevo). Reforzar con reglas de red/firewall del contenedor.
- El tamaño se comprueba tras la descarga (no corta en streaming).
- Rate limit y jobs en memoria: se pierden al reiniciar; una sola réplica.
- No se persiste contenido scrapeado; los logs no incluyen URLs, cuerpos ni claves.

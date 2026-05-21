"""ui service entry point.

Serves the built React SPA at `/` and exposes `/config` which returns runtime
config (so the same image works in any environment).

In AKS the UI also reverse-proxies `/api/*` to the in-cluster API service so
the browser sees a single origin. This avoids cross-site cookie / CORS
problems with the session cookie (which is `SameSite=Lax; Secure=false`).
Set `API_INTERNAL_URL` to the in-cluster API DNS name to enable the proxy.
"""
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="BankBuddy UI", version="0.1.0-skeleton")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

API_INTERNAL_URL = os.getenv("API_INTERNAL_URL", "").rstrip("/")
_api_client: httpx.AsyncClient | None = None
if API_INTERNAL_URL:
    _api_client = httpx.AsyncClient(base_url=API_INTERNAL_URL, timeout=60.0)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ui", "phase": "1a-skeleton"}


@app.get("/config")
async def config() -> JSONResponse:
    """Runtime config exposed to the React app (no secrets)."""
    return JSONResponse(
        {
            "api_base_url": os.getenv("PUBLIC_API_BASE_URL", "http://localhost:8000"),
            "auth_provider": os.getenv("AUTH_PROVIDER", "local-dev"),
        }
    )


# ---------------------------------------------------------------------------
# Reverse-proxy /api/* -> in-cluster API service. Registered BEFORE the SPA
# fallback so /api routes are not swallowed by the catch-all.
# ---------------------------------------------------------------------------
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def api_proxy(path: str, request: Request) -> Response:
    if _api_client is None:
        return Response(status_code=503, content=b"api proxy not configured")
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    upstream = await _api_client.request(
        request.method,
        f"/{path}",
        params=request.query_params,
        content=body,
        headers=headers,
        cookies=request.cookies,
    )
    resp_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )


# Serve the built React app (index.html + assets) when present.
if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        # SPA routing: any unknown path returns index.html
        return FileResponse(STATIC_DIR / "index.html")

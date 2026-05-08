"""ui service entry point - Phase 1a placeholder.

Serves the built React SPA at `/` and exposes a small `/config` endpoint
the SPA reads at runtime (so the same image works in any environment).

The UI never calls the agent or banking services directly; it only calls
the `api` service.
"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="BankBuddy UI", version="0.1.0-skeleton")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


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


# Serve the built React app (index.html + assets) when present.
if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        # SPA routing: any unknown path returns index.html
        return FileResponse(STATIC_DIR / "index.html")

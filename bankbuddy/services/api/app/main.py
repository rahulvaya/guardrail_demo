"""API gateway entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .agent_client import AgentClient
from .auth.factory import build_auth
from .routers import router
from .settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "api boot: auth=%s agent=%s",
        settings.auth_provider,
        settings.agent_internal_url,
    )
    app.state.auth = build_auth(settings)
    app.state.agent_client = AgentClient(
        base_url=settings.agent_internal_url,
        internal_token=settings.agent_internal_token,
    )
    yield


app = FastAPI(title="BankBuddy API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ui_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name, "auth_provider": settings.auth_provider}


app.include_router(router)

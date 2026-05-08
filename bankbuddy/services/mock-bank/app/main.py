"""Mock bank service entrypoint.

On startup:
  1. Ensures `bank` schema tables exist (the schema itself is created by init.sql).
  2. Seeds demo data idempotently.

Then mounts the routers.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import SessionLocal, engine
from .models import Base
from .routers import router as bank_router
from .seed import seed
from .settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mock-bank")

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("creating tables in `bank` schema if missing")
    Base.metadata.create_all(bind=engine)
    if settings.seed_on_startup:
        with SessionLocal() as session:
            seed(session)
    yield


app = FastAPI(title="BankBuddy Mock Bank", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


app.include_router(bank_router)

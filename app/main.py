"""FastAPI app and router registration."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import ROOT, get_settings
from app.database import init_db
from app.routers import customer, driver, shipments
from app.services.llm import provider_available

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
STATIC = Path(__file__).parent / "static"
DRIVER_APP = ROOT / "driver_app"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    get_settings().storage_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="RouteException API",
    version="1.0.0",
    description="Multimodal assistant for last-mile delivery exceptions. Code decides facts and rules; "
    "the AI reads unstructured inputs and drafts text; a human approves every outgoing message.",
    lifespan=lifespan,
)
app.include_router(shipments.router)
app.include_router(shipments.audit_router)
app.include_router(driver.router)
app.include_router(customer.router)
app.mount("/driver-static", StaticFiles(directory=DRIVER_APP), name="driver-static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> str:
    return (STATIC / "home.html").read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict[str, object]:
    s = get_settings()
    return {"ok": True, "llm_provider": s.llm_provider, "llm_live": provider_available()}


@app.get("/driver", include_in_schema=False)
def driver_page() -> FileResponse:
    return FileResponse(DRIVER_APP / "index.html")


@app.get("/track/{tracking_id}", include_in_schema=False)
def track_page(tracking_id: str) -> FileResponse:  # noqa: ARG001  (read client-side)
    return FileResponse(STATIC / "track.html")

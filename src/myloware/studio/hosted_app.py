"""Vercel entry point exposing only the recorded Studio and its visitor UI."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from myloware.api.routes.studio import router
from myloware.config.studio import get_studio_settings
from myloware.studio.hosted_recorded import recorded_bundle

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "web/demo"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # No legacy app startup, provider probing, ingestion or local worker loop.
    recorded_bundle(get_studio_settings())
    yield


app = FastAPI(title="AISMR", docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(router)


@app.middleware("http")
async def response_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self' https://mjames.dev https://www.mjames.dev"
    )
    if request.url.path.startswith("/v1/"):
        response.headers["Cache-Control"] = "private, no-store"
    return response


@app.get("/health")
async def health():
    from sqlalchemy import text

    from myloware.storage.database import get_async_session_factory

    recorded_bundle(get_studio_settings())
    async with get_async_session_factory()() as session:
        await session.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "name": "AISMR",
        "mode": "recorded",
        "generation": False,
        "rendering": False,
        "posting": False,
    }


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="studio-ui")

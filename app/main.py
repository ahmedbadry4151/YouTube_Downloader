from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router as api_router
from .config import Settings, get_settings
from .events import EventBus
from .manager import JobManager
from .store import Store

log = logging.getLogger("turbotube")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.prepare_dirs()
    _configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        bus = EventBus()
        bus.attach_loop(loop)
        store = Store(settings.data_path / "turbotube.db")
        manager = JobManager(settings, store, bus)
        app.state.settings = settings
        app.state.bus = bus
        app.state.store = store
        app.state.manager = manager
        log.info("TurboTube %s ready - downloads: %s", __version__, settings.download_path)
        if settings.auto_resume:
            resumed = manager.resume_interrupted()
            if resumed:
                log.info("Auto-resumed %s item(s)", resumed)
        try:
            yield
        finally:
            manager.shutdown()
            store.close()

    app = FastAPI(
        title="TurboTube",
        version=__version__,
        description="High-performance YouTube downloader with parallel playlists and SSE progress.",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def security_and_auth(request: Request, call_next):
        path = request.url.path
        if settings.api_key and path.startswith("/api") and path != "/api/health":
            supplied = (
                request.headers.get("x-api-key")
                or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                or request.query_params.get("key", "")
            )
            if not supplied or not hmac.compare_digest(supplied, settings.api_key):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing API key"},
                    headers={"WWW-Authenticate": "Bearer"},
                )

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not path.startswith(("/docs", "/openapi.json", "/redoc")):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' https: data:; media-src 'self' https: blob:; "
                "connect-src 'self'; style-src 'self'; script-src 'self'; font-src 'self'",
            )
        return response

    app.include_router(api_router)

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="ui")
    else:  # pragma: no cover - defensive
        @app.get("/")
        async def fallback() -> dict[str, str]:
            return {"app": "TurboTube", "message": "UI assets are missing"}

    return app


app = create_app()

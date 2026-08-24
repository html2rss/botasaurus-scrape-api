"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.openapi import configure_openapi
from app.config import Settings, get_settings
from app.engine import ScraperEngine
from app.infra.sentry import flush_sentry, setup_sentry
from app.logging_config import setup_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    setup_logging()
    setup_sentry(resolved_settings)
    openapi_metadata = configure_openapi(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.settings = resolved_settings
        app.state.engine = ScraperEngine(settings=resolved_settings)
        app.state.executor = ThreadPoolExecutor(
            max_workers=max(1, resolved_settings.scrape_max_workers)
        )
        yield
        app.state.executor.shutdown(wait=False, cancel_futures=True)
        flush_sentry()

    from app.api.routes import health, scrape

    app = FastAPI(
        title="Botasaurus Scrape API",
        description=openapi_metadata.api_description.strip(),
        version="2.0.0",
        contact=openapi_metadata.contact,
        license_info=openapi_metadata.license_info,
        servers=openapi_metadata.servers,
        openapi_tags=openapi_metadata.openapi_tags,
        lifespan=lifespan,
    )

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(scrape.router)

    return app


app = create_app()

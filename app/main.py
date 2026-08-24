"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.openapi import (
    API_DESCRIPTION,
    CONTACT,
    LICENSE_INFO,
    OPENAPI_TAGS,
    SERVERS,
)
from app.api.routes import health, scrape
from app.config import Settings, get_settings
from app.infra.sentry import flush_sentry, setup_sentry
from app.logging_config import setup_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    setup_logging()
    setup_sentry(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        yield
        executor = getattr(app.state, "executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        flush_sentry()

    app = FastAPI(
        title="Botasaurus Scrape API",
        description=API_DESCRIPTION.strip(),
        version="2.0.0",
        contact=CONTACT,
        license_info=LICENSE_INFO,
        servers=SERVERS,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(scrape.router)

    return app


app = create_app()

"""FastAPI application factory."""

import logging
import platform
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.openapi import configure_openapi
from app.config import Settings, get_settings
from app.engine import ScraperEngine
from app.engine.warm_pool import WarmDriverPool, create_driver_from_fingerprint
from app.infra.runtime_stats import RuntimeStatsReporter, run_runtime_preflight
from app.infra.sentry import flush_sentry, setup_sentry
from app.infra.version import botasaurus_version
from app.logging_config import get_logger, log_event, setup_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    setup_logging(log_format=resolved_settings.log_format.value)
    setup_sentry(resolved_settings)
    openapi_metadata = configure_openapi(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.settings = resolved_settings
        engine = ScraperEngine(settings=resolved_settings)
        if resolved_settings.scrape_prewarm:
            engine.warm_pool = WarmDriverPool(
                runtime_root=engine.runtime_root,
                idle_ttl_seconds=resolved_settings.scrape_prewarm_idle_ttl_seconds,
                min_refill_seconds=float(
                    resolved_settings.scrape_prewarm_min_refill_seconds
                ),
                driver_factory=create_driver_from_fingerprint,
            )
        app.state.engine = engine
        app.state.executor = ThreadPoolExecutor(
            max_workers=max(1, resolved_settings.scrape_max_workers)
        )
        run_runtime_preflight(runtime_root=engine.runtime_root)
        log_event(
            get_logger(),
            logging.INFO,
            "service_started",
            release=resolved_settings.sentry.release.strip() or None,
            botasaurus_version=botasaurus_version(),
            python=platform.python_version(),
            prewarm=resolved_settings.scrape_prewarm,
            max_workers=resolved_settings.scrape_max_workers,
            max_per_host=resolved_settings.scrape_max_per_host,
            timeout_s=resolved_settings.scrape_timeout_seconds,
            work_timeout_s=resolved_settings.scrape_work_timeout_seconds,
            log_format=resolved_settings.log_format.value,
            sentry=bool(resolved_settings.sentry.dsn.strip()),
        )
        stats = RuntimeStatsReporter(
            interval_seconds=float(
                resolved_settings.scrape_runtime_stats_interval_seconds
            ),
            runtime_root=engine.runtime_root,
            active_requests=engine.active_request_count,
            warm_present=lambda: (
                engine.warm_pool.has_ready_spare()
                if engine.warm_pool is not None
                else False
            ),
        )
        app.state.runtime_stats = stats
        stats.start()
        yield
        stats.stop()
        if engine.warm_pool is not None:
            engine.warm_pool.shutdown()
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
    app.include_router(health.create_router())
    app.include_router(scrape.create_router())

    return app


app = create_app()

"""FastAPI dependency providers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from fastapi import Depends, Header, Request

from app.config import Settings, get_settings
from app.domain.scrape_service import ScrapeService
from app.engine import ScraperEngine
from app.infra.request_id import resolve_request_id


def get_settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def get_executor(settings: SettingsDep) -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=max(1, settings.scrape_max_workers))


ExecutorDep = Annotated[ThreadPoolExecutor, Depends(get_executor)]


def get_engine(settings: SettingsDep) -> ScraperEngine:
    return ScraperEngine(settings=settings)


EngineDep = Annotated[ScraperEngine, Depends(get_engine)]


def get_scrape_service(
    settings: SettingsDep,
    engine: EngineDep,
    executor: ExecutorDep,
) -> ScrapeService:
    return ScrapeService(settings=settings, engine=engine, executor=executor)


ScrapeServiceDep = Annotated[ScrapeService, Depends(get_scrape_service)]


def resolve_scrape_request_id(
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    *,
    host: str | None = None,
) -> str:
    request_id, _ = resolve_request_id(x_request_id, host=host)
    return request_id


def resolve_request_id_from_request(request: Request) -> str:
    body = getattr(request.state, "validation_body", None)
    url = body.get("url") if isinstance(body, dict) else None
    host = None
    if url:
        from urllib.parse import urlparse

        host = urlparse(str(url)).hostname
    return resolve_scrape_request_id(
        request.headers.get("X-Request-Id"),
        host=host,
    )

"""FastAPI dependency providers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from fastapi import Depends, Header, Request

from app.config import Settings
from app.domain.scrape_service import ScrapeService
from app.engine import ScraperEngine
from app.infra.request_id import resolve_request_id


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_executor(request: Request) -> ThreadPoolExecutor:
    return request.app.state.executor


ExecutorDep = Annotated[ThreadPoolExecutor, Depends(get_executor)]


def get_engine(request: Request) -> ScraperEngine:
    return request.app.state.engine


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

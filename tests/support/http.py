"""Shared HTTP test helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol, cast

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.engine import ScraperEngine
from app.infra.scrape_progress import ScrapeProgress
from app.main import create_app
from app.schemas.request import ScrapeRequest
from app.schemas.response import ScrapeError, ScrapeSuccess


class ExecuteSideEffect(Protocol):
    def __call__(
        self,
        payload: ScrapeRequest,
        deadline_monotonic: float | None = ...,
        *,
        request_id: str | None = ...,
        progress: ScrapeProgress | None = ...,
    ) -> ScrapeSuccess | ScrapeError: ...


class _EngineExecuteProxy:
    def __init__(
        self,
        engine: ScraperEngine,
        execute: ExecuteSideEffect,
    ) -> None:
        self._engine = engine
        self.execute = execute

    def __getattr__(self, name: str) -> object:
        return getattr(self._engine, name)


@contextmanager
def test_client(
    *,
    engine: ScraperEngine | None = None,
    execute_side_effect: ExecuteSideEffect | None = None,
) -> Iterator[TestClient]:
    app: FastAPI = create_app()
    bound_engine: ScraperEngine | _EngineExecuteProxy | None = engine

    if bound_engine is not None:

        def _override_engine(_request: Request) -> ScraperEngine:
            return cast(ScraperEngine, bound_engine)

        app.dependency_overrides[get_engine] = _override_engine

    with TestClient(app) as client:
        if bound_engine is None:
            bound_engine = client.app.state.engine
        if execute_side_effect is not None:
            proxy_base = cast(ScraperEngine, bound_engine)
            bound_engine = _EngineExecuteProxy(proxy_base, execute_side_effect)

            def _override_engine_with_proxy(_request: Request) -> ScraperEngine:
                return cast(ScraperEngine, bound_engine)

            app.dependency_overrides[get_engine] = _override_engine_with_proxy
        yield client
    app.dependency_overrides.clear()

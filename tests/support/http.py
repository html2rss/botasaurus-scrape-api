"""Shared HTTP test helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

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
    resolved_engine = engine

    if resolved_engine is not None:

        def _override_engine(request: Request) -> ScraperEngine:
            return resolved_engine

        app.dependency_overrides[get_engine] = _override_engine

    with TestClient(app) as client:
        if resolved_engine is None:
            resolved_engine = client.app.state.engine
        if execute_side_effect is not None:
            resolved_engine = _EngineExecuteProxy(resolved_engine, execute_side_effect)

            def _override_engine_with_proxy(request: Request) -> ScraperEngine:
                return resolved_engine  # type: ignore[return-value]

            app.dependency_overrides[get_engine] = _override_engine_with_proxy
        yield client
    app.dependency_overrides.clear()

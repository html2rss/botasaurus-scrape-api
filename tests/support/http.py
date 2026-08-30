from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.config import Settings
from app.engine import ScraperEngine
from app.engine.work_lease import WorkLease
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
        lease: WorkLease | None = ...,
    ) -> ScrapeSuccess | ScrapeError: ...


class _EngineExecuteProxy(ScraperEngine):
    def __init__(
        self,
        engine: ScraperEngine,
        execute_fn: ExecuteSideEffect,
    ) -> None:
        super().__init__(settings=engine.settings, runtime_root=engine.runtime_root)
        self._engine = engine
        self._execute_fn = execute_fn

    def execute(
        self,
        payload: ScrapeRequest,
        deadline_monotonic: float | None = None,
        *,
        request_id: str | None = None,
        lease: WorkLease | None = None,
    ) -> ScrapeSuccess | ScrapeError:
        return self._execute_fn(
            payload,
            deadline_monotonic,
            request_id=request_id,
            lease=lease,
        )


@contextmanager
def test_client(
    *,
    settings: Settings | None = None,
    engine: ScraperEngine | None = None,
    execute_side_effect: ExecuteSideEffect | None = None,
) -> Iterator[TestClient]:
    app: FastAPI = create_app(settings)
    bound_engine: ScraperEngine | None = engine

    if bound_engine is not None:
        engine_override = bound_engine

        def _override_engine(_request: Request) -> ScraperEngine:
            return engine_override

        app.dependency_overrides[get_engine] = _override_engine

    with TestClient(app) as client:
        if bound_engine is None:
            raw_engine: object = getattr(client.app.state, "engine", None)
            if isinstance(raw_engine, ScraperEngine):
                bound_engine = raw_engine
        if execute_side_effect is not None and bound_engine is not None:
            proxy_engine = _EngineExecuteProxy(bound_engine, execute_side_effect)

            def _override_engine_with_proxy(_request: Request) -> ScraperEngine:
                return proxy_engine

            app.dependency_overrides[get_engine] = _override_engine_with_proxy
        yield client
    app.dependency_overrides.clear()

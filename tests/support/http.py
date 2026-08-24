"""Shared HTTP test helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.deps import get_engine
from app.engine import ScraperEngine
from app.main import create_app


@contextmanager
def test_client(
    *,
    engine: ScraperEngine | None = None,
    execute_side_effect: Callable[..., Any] | None = None,
) -> Iterator[TestClient]:
    app: FastAPI = create_app()

    if engine is not None:

        def _override_engine(request: Request) -> ScraperEngine:
            return engine

        app.dependency_overrides[get_engine] = _override_engine

    with TestClient(app) as client:
        resolved_engine = engine or client.app.state.engine
        if execute_side_effect is not None:
            resolved_engine.execute = execute_side_effect  # type: ignore[method-assign]
        yield client
    app.dependency_overrides.clear()

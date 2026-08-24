"""Shared HTTP test helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
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
    resolved_engine = engine or ScraperEngine()

    if execute_side_effect is not None:
        resolved_engine.execute = execute_side_effect  # type: ignore[method-assign]

    app.dependency_overrides[get_engine] = lambda: resolved_engine
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()

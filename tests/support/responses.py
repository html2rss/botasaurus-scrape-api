"""Typed wrappers for HTTP assertions in tests."""

from __future__ import annotations

from typing import Any

from httpx import Response
from starlette.testclient import TestClient


def response_json(response: Response) -> dict[str, Any]:
    return response.json()


def post_scrape(
    client: TestClient,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    response = client.post("/scrape", json=payload, headers=headers)
    return response.status_code, response_json(response)

"""Validated test constructors for scrape request wire types."""

from __future__ import annotations

from typing import Any, cast

from pydantic import HttpUrl

from app.schemas.request import ScrapeRequest

EXAMPLE_URL = "https://example.com"


def example_url() -> HttpUrl:
    return cast(HttpUrl, EXAMPLE_URL)


def scrape_request(**kwargs: Any) -> ScrapeRequest:
    payload = {"url": EXAMPLE_URL, **kwargs}
    return ScrapeRequest.model_validate(payload)

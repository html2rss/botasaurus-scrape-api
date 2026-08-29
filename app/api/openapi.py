"""OpenAPI metadata shared by route modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from app.api.openapi_examples import (
    HEALTH_EXAMPLE,
    SCRAPE_ERROR_EXAMPLE,
    SCRAPE_SUCCESS_EXAMPLE,
)
from app.config import Settings
from app.schemas.response import ScrapeError


@dataclass(frozen=True, slots=True)
class OpenApiMetadata:
    api_description: str
    json_error_example: dict[str, dict[str, dict[str, Any]]]
    scrape_error_responses: dict[int | str, dict[str, Any]]
    health_responses: dict[int | str, dict[str, Any]]
    scrape_success_response: dict[int | str, dict[str, Any]]
    openapi_tags: list[dict[str, str]]
    servers: list[dict[str, str]]
    contact: dict[str, str]
    license_info: dict[str, str]


def build_openapi_metadata(settings: Settings) -> OpenApiMetadata:
    work_timeout = settings.scrape_work_timeout_seconds
    api_description = f"""
Docker-first scrape API that uses Botasaurus to fetch rendered HTML.

- `GET /health` — liveness and detected Botasaurus version
- `POST /scrape` — scrape a public `http`/`https` URL

`wait_timeout_seconds` values outside `[1, {work_timeout}]`
are **clamped** into that range so scrape still runs; they are not rejected
with 422.

When `html` is present it is UTF-8-normalized and `headers` `content-type` is
`text/html; charset=utf-8`.

Localhost, private, link-local, multicast, reserved, and unspecified
destinations are blocked (403). Schema validation failures use this API's
scrape error envelope, not FastAPI `detail`.
"""
    json_error_example = {"application/json": {"example": SCRAPE_ERROR_EXAMPLE}}
    scrape_error_responses = {
        400: {
            "model": ScrapeError,
            "description": "URL rejected by validation (scheme, host, or unresolvable target).",
            "content": json_error_example,
        },
        403: {
            "model": ScrapeError,
            "description": "URL blocked by SSRF guardrails (localhost, private, or reserved destination).",
            "content": json_error_example,
        },
        422: {
            "model": ScrapeError,
            "description": "Request schema validation failed. Body is the scrape error envelope, not FastAPI `detail`.",
            "content": json_error_example,
        },
        502: {
            "model": ScrapeError,
            "description": "Scrape execution failure or challenge block after the final attempt.",
            "content": {
                "application/json": {
                    "example": {
                        **SCRAPE_ERROR_EXAMPLE,
                        "error": "Bot challenge detected (Just a moment...)",
                        "error_category": "challenge_block",
                        "diagnostics": {
                            **SCRAPE_ERROR_EXAMPLE["diagnostics"],
                            "attempts": 3,
                            "strategy_used": "get",
                            "render_ms": 1500,
                            "execution_tier": "browser_driver",
                            "challenge": {
                                "blocked": True,
                                "detected": True,
                                "marker": "Just a moment...",
                            },
                        },
                    }
                }
            },
        },
        504: {
            "model": ScrapeError,
            "description": "Scrape timed out before a result was produced.",
            "content": {
                "application/json": {
                    "example": {
                        **SCRAPE_ERROR_EXAMPLE,
                        "error": "Page navigate/wait exceeded budget",
                        "error_category": "timeout",
                        "diagnostics": {
                            **SCRAPE_ERROR_EXAMPLE["diagnostics"],
                            "attempts": 1,
                            "strategy_used": "get",
                            "render_ms": 45012,
                            "execution_tier": "browser_driver",
                            "timeout_phase": "work",
                        },
                    }
                }
            },
        },
    }
    return OpenApiMetadata(
        api_description=api_description,
        json_error_example=json_error_example,
        scrape_error_responses=cast(
            dict[int | str, dict[str, Any]], scrape_error_responses
        ),
        health_responses={
            200: {
                "description": "Service is up.",
                "content": {"application/json": {"example": HEALTH_EXAMPLE}},
            }
        },
        scrape_success_response={
            200: {
                "description": "Rendered HTML plus diagnostics. `html` is UTF-8-normalized.",
                "content": {"application/json": {"example": SCRAPE_SUCCESS_EXAMPLE}},
            }
        },
        openapi_tags=[
            {
                "name": "health",
                "description": "Liveness probe and detected Botasaurus package version.",
            },
            {
                "name": "scrape",
                "description": "Render a public URL and return UTF-8 HTML plus diagnostics.",
            },
        ],
        servers=[
            {
                "url": "http://localhost:4010",
                "description": "Local Docker (make serve)",
            }
        ],
        contact={
            "name": "html2rss",
            "url": "https://github.com/html2rss/botasaurus-scrape-api/issues",
        },
        license_info={
            "name": "MIT",
            "url": "https://opensource.org/licenses/MIT",
        },
    )


_registry: OpenApiMetadata | None = None


def configure_openapi(settings: Settings) -> OpenApiMetadata:
    global _registry
    _registry = build_openapi_metadata(settings)
    return _registry


def get_openapi_metadata() -> OpenApiMetadata:
    if _registry is None:
        from app.config import get_settings

        return configure_openapi(get_settings())
    return _registry


def get_scrape_error_responses() -> dict[int | str, dict[str, Any]]:
    return get_openapi_metadata().scrape_error_responses


def get_scrape_success_response() -> dict[int | str, dict[str, Any]]:
    return get_openapi_metadata().scrape_success_response


def get_health_responses() -> dict[int | str, dict[str, Any]]:
    return get_openapi_metadata().health_responses

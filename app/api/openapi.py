"""OpenAPI metadata shared by route modules."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.schemas import (
    HEALTH_EXAMPLE,
    SCRAPE_ERROR_EXAMPLE,
    SCRAPE_SUCCESS_EXAMPLE,
    ScrapeError,
)


@dataclass(frozen=True, slots=True)
class OpenApiMetadata:
    api_description: str
    json_error_example: dict[str, dict[str, dict]]
    scrape_error_responses: dict[int, dict]
    health_responses: dict[int, dict]
    scrape_success_response: dict[int, dict]
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
                        "error": (
                            f"Scrape timed out after {settings.scrape_timeout_seconds} "
                            "seconds (phase=work)"
                        ),
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
        scrape_error_responses=scrape_error_responses,
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


# Populated by configure_openapi() during create_app(); route modules import these names.
API_DESCRIPTION = ""
JSON_ERROR_EXAMPLE: dict[str, dict[str, dict]] = {}
SCRAPE_ERROR_RESPONSES: dict[int, dict] = {}
HEALTH_RESPONSES: dict[int, dict] = {}
SCRAPE_SUCCESS_RESPONSE: dict[int, dict] = {}
OPENAPI_TAGS: list[dict[str, str]] = []
SERVERS: list[dict[str, str]] = []
CONTACT: dict[str, str] = {}
LICENSE_INFO: dict[str, str] = {}


def configure_openapi(settings: Settings) -> OpenApiMetadata:
    metadata = build_openapi_metadata(settings)
    global API_DESCRIPTION, JSON_ERROR_EXAMPLE, SCRAPE_ERROR_RESPONSES
    global HEALTH_RESPONSES, SCRAPE_SUCCESS_RESPONSE, OPENAPI_TAGS
    global SERVERS, CONTACT, LICENSE_INFO
    API_DESCRIPTION = metadata.api_description
    JSON_ERROR_EXAMPLE.clear()
    JSON_ERROR_EXAMPLE.update(metadata.json_error_example)
    SCRAPE_ERROR_RESPONSES.clear()
    SCRAPE_ERROR_RESPONSES.update(metadata.scrape_error_responses)
    HEALTH_RESPONSES.clear()
    HEALTH_RESPONSES.update(metadata.health_responses)
    SCRAPE_SUCCESS_RESPONSE.clear()
    SCRAPE_SUCCESS_RESPONSE.update(metadata.scrape_success_response)
    OPENAPI_TAGS.clear()
    OPENAPI_TAGS.extend(metadata.openapi_tags)
    SERVERS.clear()
    SERVERS.extend(metadata.servers)
    CONTACT.clear()
    CONTACT.update(metadata.contact)
    LICENSE_INFO.clear()
    LICENSE_INFO.update(metadata.license_info)
    return metadata

"""OpenAPI metadata shared by route modules."""

from __future__ import annotations

from app.config import get_settings
from app.schemas import (
    DEFAULT_SCRAPE_WORK_TIMEOUT_SECONDS,
    HEALTH_EXAMPLE,
    SCRAPE_ERROR_EXAMPLE,
    SCRAPE_SUCCESS_EXAMPLE,
    ScrapeError,
)

_settings = get_settings()

API_DESCRIPTION = f"""
Docker-first scrape API that uses Botasaurus to fetch rendered HTML.

- `GET /health` — liveness and detected Botasaurus version
- `POST /scrape` — scrape a public `http`/`https` URL

`wait_timeout_seconds` values outside `[1, {DEFAULT_SCRAPE_WORK_TIMEOUT_SECONDS}]`
are **clamped** into that range so scrape still runs; they are not rejected
with 422.

When `html` is present it is UTF-8-normalized and `headers` `content-type` is
`text/html; charset=utf-8`.

Localhost, private, link-local, multicast, reserved, and unspecified
destinations are blocked (403). Schema validation failures use this API's
scrape error envelope, not FastAPI `detail`.
"""

JSON_ERROR_EXAMPLE = {"application/json": {"example": SCRAPE_ERROR_EXAMPLE}}

SCRAPE_ERROR_RESPONSES = {
    400: {
        "model": ScrapeError,
        "description": "URL rejected by validation (scheme, host, or unresolvable target).",
        "content": JSON_ERROR_EXAMPLE,
    },
    403: {
        "model": ScrapeError,
        "description": "URL blocked by SSRF guardrails (localhost, private, or reserved destination).",
        "content": JSON_ERROR_EXAMPLE,
    },
    422: {
        "model": ScrapeError,
        "description": "Request schema validation failed. Body is the scrape error envelope, not FastAPI `detail`.",
        "content": JSON_ERROR_EXAMPLE,
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
                        f"Scrape timed out after {_settings.scrape_timeout_seconds} "
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

HEALTH_RESPONSES = {
    200: {
        "description": "Service is up.",
        "content": {"application/json": {"example": HEALTH_EXAMPLE}},
    }
}

SCRAPE_SUCCESS_RESPONSE = {
    200: {
        "description": "Rendered HTML plus diagnostics. `html` is UTF-8-normalized.",
        "content": {"application/json": {"example": SCRAPE_SUCCESS_EXAMPLE}},
    }
}

OPENAPI_TAGS = [
    {
        "name": "health",
        "description": "Liveness probe and detected Botasaurus package version.",
    },
    {
        "name": "scrape",
        "description": "Render a public URL and return UTF-8 HTML plus diagnostics.",
    },
]

SERVERS = [
    {
        "url": "http://localhost:4010",
        "description": "Local Docker (make serve)",
    }
]

CONTACT = {
    "name": "html2rss",
    "url": "https://github.com/html2rss/botasaurus-scrape-api/issues",
}

LICENSE_INFO = {
    "name": "MIT",
    "url": "https://opensource.org/licenses/MIT",
}

# app/main.py
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.engine import DEFAULT_SCRAPE_TIMEOUT_SECONDS, ScraperEngine
from app.schemas import (
    HEALTH_EXAMPLE,
    SCRAPE_ERROR_EXAMPLE,
    SCRAPE_SUCCESS_EXAMPLE,
    ErrorCategory,
    HealthResponse,
    ScrapeDiagnostics,
    ScrapeError,
    ScrapeRequest,
    ScrapeSuccess,
    validation_error,
)
from app.security import UrlGuard
from app.sentry import flush_sentry, setup_sentry

logger = logging.getLogger("botasaurus_scrape_api")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

setup_sentry()

_MAX_WORKERS = int(os.getenv("SCRAPE_MAX_WORKERS", "4"))
_executor = ThreadPoolExecutor(max_workers=max(1, _MAX_WORKERS))
_engine = ScraperEngine()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    yield
    _executor.shutdown(wait=False, cancel_futures=True)
    flush_sentry()


_API_DESCRIPTION = f"""
Docker-first scrape API that uses Botasaurus to fetch rendered HTML.

- `GET /health` — liveness and detected Botasaurus version
- `POST /scrape` — scrape a public `http`/`https` URL

`wait_timeout_seconds` values outside `[1, {DEFAULT_SCRAPE_TIMEOUT_SECONDS}]`
are **clamped** into that range so scrape still runs; they are not rejected
with 422.

When `html` is present it is UTF-8-normalized and `headers` `content-type` is
`text/html; charset=utf-8`.

Localhost, private, link-local, multicast, reserved, and unspecified
destinations are blocked (403). Schema validation failures use this API's
scrape error envelope, not FastAPI `detail`.
"""

_JSON_EXAMPLE = {"application/json": {"example": SCRAPE_ERROR_EXAMPLE}}
_SCRAPE_ERROR_RESPONSES = {
    400: {
        "model": ScrapeError,
        "description": "URL rejected by validation (scheme, host, or unresolvable target).",
        "content": _JSON_EXAMPLE,
    },
    403: {
        "model": ScrapeError,
        "description": "URL blocked by SSRF guardrails (localhost, private, or reserved destination).",
        "content": _JSON_EXAMPLE,
    },
    422: {
        "model": ScrapeError,
        "description": "Request schema validation failed. Body is the scrape error envelope, not FastAPI `detail`.",
        "content": _JSON_EXAMPLE,
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
                    "error": f"Scrape timed out after {DEFAULT_SCRAPE_TIMEOUT_SECONDS} seconds",
                    "error_category": "timeout",
                }
            }
        },
    },
}


app = FastAPI(
    title="Botasaurus Scrape API",
    description=_API_DESCRIPTION.strip(),
    version="2.0.0",
    contact={
        "name": "html2rss",
        "url": "https://github.com/html2rss/botasaurus-scrape-api/issues",
    },
    license_info={
        "name": "MIT",
        "url": "https://opensource.org/licenses/MIT",
    },
    servers=[
        {
            "url": "http://localhost:4010",
            "description": "Local Docker (make serve)",
        }
    ],
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
    lifespan=lifespan,
)

_NON_FIELD_LOC = {"body", "query", "path", "header"}


def _schema_field_from_loc(loc: tuple[Any, ...] | list[Any]) -> str:
    for part in loc:
        if part not in _NON_FIELD_LOC:
            return str(part)
    return str(loc[-1]) if loc else "unknown"


def _first_schema_field(errors: list[Any]) -> str:
    if not errors:
        return "unknown"
    return _schema_field_from_loc(errors[0].get("loc") or ())


def _url_from_validation_body(body: Any) -> str:
    if isinstance(body, dict) and body.get("url") is not None:
        return str(body["url"])
    return ""


def _validation_error_message(errors: list[Any]) -> str:
    if not errors:
        return "Request schema validation failed"
    parts: list[str] = []
    for err in errors:
        field = _schema_field_from_loc(err.get("loc") or ())
        parts.append(f"{field}: {err.get('msg') or 'invalid'}")
    return "; ".join(parts)


def _json(model: ScrapeSuccess | ScrapeError) -> dict[str, Any]:
    return model.model_dump(mode="json")


@app.exception_handler(RequestValidationError)
async def request_schema_validation_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = list(exc.errors())
    url = _url_from_validation_body(exc.body)
    field = _first_schema_field(errors)
    logger.info(
        "request_schema_422 host=%s field=%s",
        urlparse(url).hostname if url else None,
        field,
    )
    return JSONResponse(
        status_code=422,
        content=_json(validation_error(url, _validation_error_message(errors))),
    )


@app.get(
    "/health",
    response_model=HealthResponse,
    operation_id="get-health",
    tags=["health"],
    summary="Health",
    description="Return liveness status, service name, and the installed Botasaurus version.",
    responses={
        200: {
            "description": "Service is up.",
            "content": {"application/json": {"example": HEALTH_EXAMPLE}},
        }
    },
)
def health() -> HealthResponse:
    try:
        botasaurus_version = version("botasaurus")
    except PackageNotFoundError:
        botasaurus_version = "unknown"

    return HealthResponse(
        status="ok",
        service="botasaurus-scrape-api",
        botasaurus_version=botasaurus_version,
    )


@app.post(
    "/scrape",
    response_model=ScrapeSuccess,
    responses={
        200: {
            "description": "Rendered HTML plus diagnostics. `html` is UTF-8-normalized.",
            "content": {"application/json": {"example": SCRAPE_SUCCESS_EXAMPLE}},
        },
        **_SCRAPE_ERROR_RESPONSES,
    },
    operation_id="scrape-url",
    tags=["scrape"],
    summary="Scrape a URL",
    description=(
        "Fetch rendered HTML for a public http(s) URL. Invalid or blocked "
        "targets return `ScrapeError`. `wait_timeout_seconds` is clamped, not 422."
    ),
)
async def scrape(payload: ScrapeRequest) -> JSONResponse:
    target_url = str(payload.url)
    target_validation = UrlGuard.validate(target_url)
    if not target_validation.is_allowed:
        return JSONResponse(
            status_code=target_validation.status_code,
            content=_json(
                validation_error(
                    target_url,
                    target_validation.error_message or "Target URL is blocked",
                )
            ),
        )

    if payload.proxy:
        proxy_validation = UrlGuard.validate_proxy(str(payload.proxy))
        if not proxy_validation.is_allowed:
            return JSONResponse(
                status_code=proxy_validation.status_code,
                content=_json(
                    validation_error(
                        target_url,
                        proxy_validation.error_message
                        or "Proxy URL is invalid or blocked",
                    )
                ),
            )

    started_monotonic = time.monotonic()
    deadline_monotonic = started_monotonic + DEFAULT_SCRAPE_TIMEOUT_SECONDS

    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                _executor, _engine.execute, payload, deadline_monotonic
            ),
            timeout=DEFAULT_SCRAPE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        render_ms = int((time.monotonic() - started_monotonic) * 1000)
        timeout_result = ScrapeError(
            url=target_url,
            error=f"Scrape timed out after {DEFAULT_SCRAPE_TIMEOUT_SECONDS} seconds",
            error_category=ErrorCategory.TIMEOUT,
            diagnostics=ScrapeDiagnostics(
                request_id=str(uuid.uuid4()),
                attempts=0,
                render_ms=render_ms,
            ),
        )
        logger.warning(
            "scrape_timeout host=%s mode=%s timeout_seconds=%d",
            urlparse(target_url).hostname,
            payload.navigation_mode,
            DEFAULT_SCRAPE_TIMEOUT_SECONDS,
        )
        return JSONResponse(status_code=504, content=_json(timeout_result))

    status_code = 200 if isinstance(result, ScrapeSuccess) else 502
    logger.info(
        "scrape_complete request_id=%s host=%s mode=%s tier=%s attempts=%s status=%d error_category=%s",
        result.diagnostics.request_id,
        urlparse(target_url).hostname,
        payload.navigation_mode,
        result.diagnostics.execution_tier,
        result.diagnostics.attempts,
        status_code,
        result.error_category if isinstance(result, ScrapeError) else None,
    )
    return JSONResponse(status_code=status_code, content=_json(result))

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
from pydantic import BaseModel

from app.engine import (
    DEFAULT_SCRAPE_TIMEOUT_SECONDS,
    ScraperEngine,
    ScrapeRequest,
    ScrapeResponse,
    make_error_payload,
    make_validation_error_payload,
)
from app.security import UrlGuard

_MAX_WORKERS = int(os.getenv("SCRAPE_MAX_WORKERS", "4"))
_executor = ThreadPoolExecutor(max_workers=max(1, _MAX_WORKERS))
_engine = ScraperEngine()

logger = logging.getLogger("botasaurus_scrape_api")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    yield
    _executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(
    title="Botasaurus Scrape API",
    version="1.3.0",
    lifespan=lifespan,
)

_NON_FIELD_LOC = {"body", "query", "path", "header"}
_SCRAPE_ERROR_RESPONSES = {
    400: {
        "model": ScrapeResponse,
        "description": "URL rejected by validation",
    },
    403: {
        "model": ScrapeResponse,
        "description": "URL blocked by SSRF guardrails",
    },
    422: {
        "model": ScrapeResponse,
        "description": "Request schema validation failed",
    },
    502: {
        "model": ScrapeResponse,
        "description": "Scrape execution failure",
    },
    504: {
        "model": ScrapeResponse,
        "description": "Scrape timed out",
    },
}


class HealthResponse(BaseModel):
    status: str
    service: str
    botasaurus_version: str


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
        content=make_validation_error_payload(url, _validation_error_message(errors)),
    )


@app.get("/health", response_model=HealthResponse)
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
    response_model=ScrapeResponse,
    responses=_SCRAPE_ERROR_RESPONSES,
)
async def scrape(payload: ScrapeRequest) -> JSONResponse:
    target_url = str(payload.url)
    target_validation = UrlGuard.validate(target_url)
    if not target_validation.is_allowed:
        return JSONResponse(
            status_code=target_validation.status_code,
            content=make_validation_error_payload(
                target_url,
                target_validation.error_message or "Target URL is blocked",
            ),
        )

    if payload.proxy:
        proxy_validation = UrlGuard.validate_proxy(str(payload.proxy))
        if not proxy_validation.is_allowed:
            return JSONResponse(
                status_code=proxy_validation.status_code,
                content=make_validation_error_payload(
                    target_url,
                    proxy_validation.error_message or "Proxy URL is invalid or blocked",
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
        timeout_result = make_error_payload(
            target_url,
            f"Scrape timed out after {DEFAULT_SCRAPE_TIMEOUT_SECONDS} seconds",
            request_id=str(uuid.uuid4()),
            attempts=0,
            strategy_used=None,
            render_ms=render_ms,
            error_category="timeout",
        )
        logger.warning(
            "scrape_timeout host=%s mode=%s timeout_seconds=%d",
            urlparse(target_url).hostname,
            payload.navigation_mode,
            DEFAULT_SCRAPE_TIMEOUT_SECONDS,
        )
        return JSONResponse(status_code=504, content=timeout_result)

    status_code = 200 if not result.get("error") else 502
    logger.info(
        "scrape_complete request_id=%s host=%s mode=%s tier=%s attempts=%s status=%d error_category=%s",
        result.get("request_id"),
        urlparse(target_url).hostname,
        payload.navigation_mode,
        result.get("execution_tier"),
        result.get("attempts"),
        status_code,
        result.get("error_category"),
    )
    return JSONResponse(status_code=status_code, content=result)

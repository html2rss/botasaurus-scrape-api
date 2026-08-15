# app/main.py
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any, AsyncGenerator, Optional
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.detector import ChallengeAssessment, ChallengeDetector
from app.engine import (
    DEFAULT_SCRAPE_TIMEOUT_SECONDS,
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    ErrorCategory,
    ExecutionMode,
    NavigationMode,
    ScrapeRequest,
    ScrapeResponse,
    ScraperEngine,
    make_error_payload,
    make_validation_error_payload,
)
from app.metadata import MetadataExtractor, MetadataResult
from app.security import UrlGuard, ValidationResult

# Re-exports and aliases for backward compatibility with existing tests/consumers
from botasaurus.browser import Driver  # noqa: F401

_MAX_WORKERS = int(os.getenv("SCRAPE_MAX_WORKERS", "4"))
_executor = ThreadPoolExecutor(max_workers=max(1, _MAX_WORKERS))
_engine = ScraperEngine()
_RUNTIME_ROOT = _engine.runtime_root

logger = logging.getLogger("botasaurus_scrape_api")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    yield
    _executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(
    title="Botasaurus Scrape API",
    version="1.2.0",
    lifespan=lifespan,
)


# Backward-compatible function shims for legacy callers/tests
def _is_blocked_ip(ip: Any) -> bool:
    return UrlGuard.is_blocked_ip(ip)


def _validate_target_url(raw_url: str) -> tuple[bool, int, Optional[str]]:
    res = UrlGuard.validate(raw_url)
    return res.is_allowed, res.status_code, res.error_message


def _strategies_for_request(mode: NavigationMode, max_retries: int) -> list[str]:
    return ScraperEngine.resolve_strategies(mode, max_retries)


def _detect_block_challenge(
    html: str, status_code: Optional[int]
) -> tuple[bool, bool, Optional[str]]:
    assessment = ChallengeDetector.detect(html, status_code)
    return (
        assessment.blocked_detected,
        assessment.challenge_detected,
        assessment.detected_marker,
    )


def _extract_passive_metadata(
    driver: Any, target_url: str
) -> tuple[Optional[int], Optional[dict[str, str]], Optional[str]]:
    res = MetadataExtractor.extract_from_requests(driver, target_url)
    if res[0] is not None:
        return res
    return MetadataExtractor.extract_from_cdp_logs(driver, target_url)


def _fetch_metadata(
    driver: Any, target_url: str
) -> tuple[Optional[int], Optional[dict[str, str]], str, Optional[str]]:
    res = MetadataExtractor.fetch(driver, target_url)
    return res.status_code, res.headers, res.final_url, res.metadata_error


def _error_payload(
    url: str,
    message: str,
    *,
    request_id: str,
    attempts: int = 0,
    strategy_used: Optional[str] = None,
    render_ms: int = 0,
    error_category: Optional[ErrorCategory] = None,
    execution_tier: Optional[str] = None,
    detected_challenge: Optional[str] = None,
) -> dict[str, Any]:
    return make_error_payload(
        url,
        message,
        request_id=request_id,
        attempts=attempts,
        strategy_used=strategy_used,
        render_ms=render_ms,
        error_category=error_category,
        execution_tier=execution_tier,
        detected_challenge=detected_challenge,
    )


def _validation_error_payload(url: str, message: str) -> dict[str, Any]:
    return make_validation_error_payload(url, message)


def _run_scrape(
    payload: ScrapeRequest, deadline_monotonic: Optional[float] = None
) -> dict[str, Any]:
    return _engine.execute(payload, deadline_monotonic)


@app.get("/health")
def health() -> dict[str, str]:
    try:
        botasaurus_version = version("botasaurus")
    except PackageNotFoundError:
        botasaurus_version = "unknown"

    return {
        "status": "ok",
        "service": "botasaurus-scrape-api",
        "botasaurus_version": botasaurus_version,
    }


@app.post("/scrape", response_model=ScrapeResponse)
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
    except asyncio.TimeoutError:
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

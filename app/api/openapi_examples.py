"""OpenAPI response examples built from Pydantic model instances."""

from __future__ import annotations

from app.constants import SERVICE_NAME
from app.schemas.enums import ErrorCategory, ExecutionTier
from app.schemas.response import (
    ChallengeSignal,
    HealthResponse,
    ScrapeDiagnostics,
    ScrapeError,
    ScrapeSuccess,
)


def build_scrape_success_example() -> dict:
    return ScrapeSuccess(
        url="https://example.com",
        final_url="https://example.com/",
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        html="<!doctype html><html><body>Example Domain</body></html>",
        metadata_error=None,
        xhr_responses=[],
        diagnostics=ScrapeDiagnostics(
            request_id="b01ef2f8-f641-4e75-8ef2-0b73f7b4f372",
            attempts=1,
            strategy_used=None,
            render_ms=154,
            execution_tier=ExecutionTier.HTTP_REQUEST,
            challenge=ChallengeSignal(blocked=False, detected=False, marker=None),
        ),
    ).model_dump(mode="json")


def build_scrape_error_example() -> dict:
    return ScrapeError(
        url="https://example.com",
        error="Target URL is blocked",
        error_category=ErrorCategory.VALIDATION,
        diagnostics=ScrapeDiagnostics(
            request_id="b01ef2f8-f641-4e75-8ef2-0b73f7b4f372",
            attempts=0,
            strategy_used=None,
            render_ms=0,
            execution_tier=None,
            challenge=None,
        ),
    ).model_dump(mode="json")


def build_health_example() -> dict:
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        botasaurus_version="4.0.91",
    ).model_dump(mode="json")


SCRAPE_SUCCESS_EXAMPLE = build_scrape_success_example()
SCRAPE_ERROR_EXAMPLE = build_scrape_error_example()
HEALTH_EXAMPLE = build_health_example()

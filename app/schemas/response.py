from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.constants import SERVICE_NAME
from app.schemas.enums import (
    ErrorCategory,
    ExecutionTier,
    NavigationMode,
    TimeoutPhase,
)


class ChallengeSignal(BaseModel):
    blocked: bool = Field(
        description="True when HTTP status or HTML markers indicate an anti-bot block.",
        examples=[False],
    )
    detected: bool = Field(
        description="True when a known challenge interstitial or driver bot-detection signal matched.",
        examples=[False],
    )
    marker: str | None = Field(
        default=None,
        description="Matched challenge marker, or null when none matched.",
        examples=["Just a moment..."],
    )


class ScrapeDiagnostics(BaseModel):
    request_id: str = Field(
        description="Unique id for this scrape attempt, used for tracing and runtime isolation.",
        examples=["b01ef2f8-f641-4e75-8ef2-0b73f7b4f372"],
    )
    attempts: int = Field(
        default=0,
        description="Number of navigation or request attempts actually performed.",
        examples=[1],
    )
    strategy_used: NavigationMode | None = Field(
        default=None,
        description=(
            "Browser navigation strategy used on the final attempt. Null for the "
            "HTTP-request tier and for failures before navigation starts."
        ),
        examples=["google_get"],
    )
    render_ms: int = Field(
        default=0,
        description="Elapsed scrape runtime in milliseconds.",
        examples=[154],
    )
    execution_tier: ExecutionTier | None = Field(
        default=None,
        description="Tier that produced this result, or null when execution never started.",
        examples=["http_request"],
    )
    challenge: ChallengeSignal | None = Field(
        default=None,
        description="Anti-bot assessment for this attempt, or null when detection did not run.",
        examples=[{"blocked": False, "detected": False, "marker": None}],
    )
    timeout_phase: TimeoutPhase | None = Field(
        default=None,
        description=(
            "When `error_category` is `timeout`, which stage burned the budget: "
            "`queue` (threadpool wait), `boot` (browser/driver start), or `work` "
            "(navigate/wait/scroll). Null for non-timeout outcomes."
        ),
        examples=["work"],
    )


class XhrResponse(BaseModel):
    url: str = Field(
        description="Sub-resource URL whose JSON body was captured.",
        examples=["https://api.example.com/items"],
    )
    status_code: int = Field(
        description="HTTP status code of the captured XHR/fetch response.",
        examples=[200],
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Allowlisted response headers. Only `content-type` is kept.",
        examples=[{"content-type": "application/json"}],
    )
    body: str = Field(
        description="Raw JSON response body as UTF-8 text.",
        examples=['{"items":[]}'],
    )


def _empty_xhr_responses() -> list[XhrResponse]:
    return []


class ScrapeSuccess(BaseModel):
    url: str = Field(
        description="Requested scrape URL as submitted.",
        examples=["https://example.com"],
    )
    final_url: str | None = Field(
        default=None,
        description="Best-effort landing URL after redirects.",
        examples=["https://example.com/"],
    )
    status_code: int | None = Field(
        default=None,
        description="Best-effort HTTP status of the main document.",
        examples=[200],
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "Best-effort response headers. When `html` is present, `content-type` "
            "is `text/html; charset=utf-8`."
        ),
        examples=[{"content-type": "text/html; charset=utf-8"}],
    )
    html: str = Field(
        description=(
            "Rendered page HTML, UTF-8-normalized. When present, headers "
            "`content-type` is `text/html; charset=utf-8`."
        ),
        examples=["<!doctype html><html><body>Example Domain</body></html>"],
    )
    metadata_error: str | None = Field(
        default=None,
        description="Metadata extraction failure message. HTML success is still returned.",
        examples=[None],
    )
    xhr_responses: list[XhrResponse] = Field(
        default_factory=_empty_xhr_responses,
        description=(
            "JSON XHR/fetch sub-resource bodies captured on the browser tier "
            "(empty on the HTTP-request tier)."
        ),
    )
    diagnostics: ScrapeDiagnostics = Field(
        description="Per-request tracing, strategy, timing, and challenge signals.",
        examples=[
            {
                "request_id": "b01ef2f8-f641-4e75-8ef2-0b73f7b4f372",
                "attempts": 1,
                "strategy_used": None,
                "render_ms": 154,
                "execution_tier": "http_request",
                "challenge": {"blocked": False, "detected": False, "marker": None},
            }
        ],
    )


class ScrapeError(BaseModel):
    url: str = Field(
        description="Requested scrape URL as submitted, or empty when the body had no URL.",
        examples=["https://example.com"],
    )
    error: str = Field(
        description="Human-readable failure message.",
        examples=["Target URL is blocked"],
    )
    error_category: ErrorCategory = Field(
        description=(
            "Closed failure class: `timeout`, `challenge_block`, `navigation_error`, "
            "`metadata_error`, or `validation`."
        ),
        examples=["validation"],
    )
    diagnostics: ScrapeDiagnostics = Field(
        description="Per-request tracing, strategy, timing, and challenge signals.",
        examples=[
            {
                "request_id": "b01ef2f8-f641-4e75-8ef2-0b73f7b4f372",
                "attempts": 0,
                "strategy_used": None,
                "render_ms": 0,
                "execution_tier": None,
                "challenge": None,
            }
        ],
    )


class HealthResponse(BaseModel):
    status: Literal["ok"] = Field(
        description="Liveness marker. Always `ok` when the process can serve requests.",
        examples=["ok"],
    )
    service: str = Field(
        description="Service identity.",
        examples=[SERVICE_NAME],
    )
    botasaurus_version: str = Field(
        description="Installed Botasaurus package version, or `unknown` if metadata is missing.",
        examples=["4.0.91"],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "service": SERVICE_NAME,
                    "botasaurus_version": "4.0.91",
                }
            ]
        }
    )


def validation_error(
    url: str, message: str, *, request_id: str | None = None
) -> ScrapeError:
    return ScrapeError(
        url=url,
        error=message,
        error_category=ErrorCategory.VALIDATION,
        diagnostics=ScrapeDiagnostics(request_id=request_id or str(uuid.uuid4())),
    )

import logging
import os
import uuid
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationInfo,
    field_validator,
)

logger = logging.getLogger("botasaurus_scrape_api")

DEFAULT_SCRAPE_TIMEOUT_SECONDS = int(os.getenv("SCRAPE_TIMEOUT_SECONDS", "20"))
DEFAULT_WAIT_TIMEOUT_SECONDS = min(15, DEFAULT_SCRAPE_TIMEOUT_SECONDS)


class ExecutionMode(StrEnum):
    AUTO = "auto"
    REQUEST = "request"
    BROWSER = "browser"


class NavigationMode(StrEnum):
    AUTO = "auto"
    GET = "get"
    GOOGLE_GET = "google_get"
    GOOGLE_GET_BYPASS = "google_get_bypass"
    ORGANIC_GET = "organic_get"


class ExecutionTier(StrEnum):
    HTTP_REQUEST = "http_request"
    BROWSER_DRIVER = "browser_driver"


class ErrorCategory(StrEnum):
    TIMEOUT = "timeout"
    CHALLENGE_BLOCK = "challenge_block"
    NAVIGATION_ERROR = "navigation_error"
    METADATA_ERROR = "metadata_error"
    VALIDATION = "validation"


class WindowSize(BaseModel):
    width: int = Field(
        ge=1,
        description="Browser viewport width in pixels.",
        examples=[1920],
    )
    height: int = Field(
        ge=1,
        description="Browser viewport height in pixels.",
        examples=[1080],
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


class ScrapeRequest(BaseModel):
    url: HttpUrl = Field(
        description=(
            "Absolute http(s) URL to scrape. Localhost and private destinations "
            "are rejected by SSRF guardrails."
        ),
        examples=["https://example.com"],
    )
    execution_mode: ExecutionMode = Field(
        default=ExecutionMode.AUTO,
        description=(
            "`auto` tries anti-detect HTTP first and escalates to the browser; "
            "`request` stays on HTTP; `browser` always uses Chromium."
        ),
        examples=["auto"],
    )
    navigation_mode: NavigationMode = Field(
        default=NavigationMode.AUTO,
        description=(
            "Browser navigation strategy. `auto` tries `google_get`, then "
            "`google_get_bypass`, then `get`."
        ),
        examples=["auto"],
    )
    max_retries: int = Field(
        default=2,
        ge=0,
        le=3,
        description="Retries after the first attempt (`attempts = 1 + max_retries`). `auto` is capped at three strategy steps.",
        examples=[2],
    )
    wait_for_selector: str | None = Field(
        default=None,
        description="CSS selector to wait for before capture. When set, execution uses the browser tier.",
        examples=["h1"],
    )
    wait_timeout_seconds: int = Field(
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        description=(
            "Selector wait timeout in seconds. Values outside "
            f"[1, {DEFAULT_SCRAPE_TIMEOUT_SECONDS}] are clamped into that "
            "range so scrape still runs; they are not rejected with 422."
        ),
        examples=[15],
    )
    scroll: bool = Field(
        default=False,
        description="Scroll to the bottom to trigger lazy-loaded content. Routes to the browser tier when true.",
        examples=[False],
    )
    block_images: bool = Field(
        default=True,
        description="Ask the driver to skip image downloads.",
        examples=[True],
    )
    block_images_and_css: bool = Field(
        default=False,
        description="Ask the driver to skip image and CSS downloads.",
        examples=[False],
    )
    block_trackers: bool = Field(
        default=True,
        description="Block tracking/ad networks and web fonts to speed up rendering.",
        examples=[True],
    )
    wait_for_complete_page_load: bool = Field(
        default=True,
        description="Wait for the driver complete-page-load signal before capture.",
        examples=[True],
    )
    user_agent: str | None = Field(
        default=None,
        description="Explicit User-Agent. Overrides a User-Agent header when both are set.",
        examples=["Mozilla/5.0 (compatible; html2rss)"],
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description="Extra HTTP headers forwarded to the request client or browser session.",
        examples=[{"Accept-Language": "en-US,en;q=0.9"}],
    )
    cookies: dict[str, str] | None = Field(
        default=None,
        description="Cookie name/value map forwarded to the request client or browser session.",
        examples=[{"session": "abc123"}],
    )
    window_size: WindowSize | None = Field(
        default=None,
        description="Browser viewport size passed to the driver.",
        examples=[{"width": 1920, "height": 1080}],
    )
    lang: str | None = Field(
        default=None,
        description="Browser language passed to the driver.",
        examples=["en-US"],
    )
    headless: bool = Field(
        default=False,
        description="Run Chromium headless. Default false uses a virtual display in Docker.",
        examples=[False],
    )
    proxy: str | None = Field(
        default=None,
        description="Proxy URL passed to the driver. Invalid or blocked proxy URLs are rejected by SSRF guardrails.",
        examples=["http://user:pass@proxy.example:8080"],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"url": "https://example.com"},
                {
                    "url": "https://example.com",
                    "execution_mode": "auto",
                    "navigation_mode": "auto",
                    "max_retries": 2,
                    "wait_for_selector": "h1",
                    "wait_timeout_seconds": 15,
                    "scroll": True,
                    "block_images": True,
                    "block_images_and_css": False,
                    "block_trackers": True,
                    "wait_for_complete_page_load": True,
                    "user_agent": "Mozilla/5.0 (compatible; html2rss)",
                    "headers": {"Accept-Language": "en-US,en;q=0.9"},
                    "cookies": {"session": "abc123"},
                    "window_size": {"width": 1920, "height": 1080},
                    "lang": "en-US",
                    "headless": False,
                    "proxy": "http://user:pass@proxy.example:8080",
                },
            ]
        }
    )

    @field_validator("wait_timeout_seconds", mode="before")
    @classmethod
    def clamp_wait_timeout_seconds(cls, value: Any, info: ValidationInfo) -> Any:
        if value is None:
            return value
        try:
            numeric = int(value)
        except (TypeError, ValueError):  # fmt: skip
            return value

        clamped = max(1, min(numeric, DEFAULT_SCRAPE_TIMEOUT_SECONDS))
        if clamped != numeric:
            url = info.data.get("url")
            logger.info(
                "request_field_clamped host=%s field=%s from=%s to=%s",
                urlparse(str(url)).hostname if url else None,
                "wait_timeout_seconds",
                numeric,
                clamped,
            )
        return clamped

    @property
    def effective_user_agent(self) -> str | None:
        if self.user_agent:
            return self.user_agent
        if self.headers:
            return self.headers.get("User-Agent") or self.headers.get("user-agent")
        return None


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
        default_factory=list,
        description=(
            "JSON XHR/fetch sub-resource bodies captured on the browser tier "
            "(empty on the HTTP-request tier)."
        ),
        examples=[[]],
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
        examples=["botasaurus-scrape-api"],
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
                    "service": "botasaurus-scrape-api",
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


SCRAPE_SUCCESS_EXAMPLE = {
    "url": "https://example.com",
    "final_url": "https://example.com/",
    "status_code": 200,
    "headers": {"content-type": "text/html; charset=utf-8"},
    "html": "<!doctype html><html><body>Example Domain</body></html>",
    "metadata_error": None,
    "xhr_responses": [],
    "diagnostics": {
        "request_id": "b01ef2f8-f641-4e75-8ef2-0b73f7b4f372",
        "attempts": 1,
        "strategy_used": None,
        "render_ms": 154,
        "execution_tier": "http_request",
        "challenge": {"blocked": False, "detected": False, "marker": None},
    },
}

SCRAPE_ERROR_EXAMPLE = {
    "url": "https://example.com",
    "error": "Target URL is blocked",
    "error_category": "validation",
    "diagnostics": {
        "request_id": "b01ef2f8-f641-4e75-8ef2-0b73f7b4f372",
        "attempts": 0,
        "strategy_used": None,
        "render_ms": 0,
        "execution_tier": None,
        "challenge": None,
    },
}

HEALTH_EXAMPLE = {
    "status": "ok",
    "service": "botasaurus-scrape-api",
    "botasaurus_version": "4.0.91",
}

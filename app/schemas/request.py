from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationInfo,
    field_validator,
)

from app.config import get_settings
from app.schemas.enums import ExecutionMode, NavigationMode

logger = logging.getLogger("botasaurus_scrape_api")


def _wait_timeout_field_description() -> str:
    work_cap = get_settings().scrape_work_timeout_seconds
    return (
        "Selector wait timeout in seconds. Values outside "
        f"[1, {work_cap}] are clamped into that "
        "range so scrape still runs; they are not rejected with 422."
    )


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
        default=15,
        description=_wait_timeout_field_description(),
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

        clamped = max(1, min(numeric, get_settings().scrape_work_timeout_seconds))
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

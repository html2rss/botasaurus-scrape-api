# app/engine.py
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from botasaurus.browser import Driver
from botasaurus.request import Request
from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.detector import ChallengeDetector
from app.metadata import MetadataExtractor

logger = logging.getLogger("botasaurus_scrape_api")

DEFAULT_SCRAPE_TIMEOUT_SECONDS = int(os.getenv("SCRAPE_TIMEOUT_SECONDS", "20"))
DEFAULT_WAIT_TIMEOUT_SECONDS = min(15, DEFAULT_SCRAPE_TIMEOUT_SECONDS)
_RUNTIME_ROOT = Path("/tmp/scrape")

ExecutionMode = Literal["auto", "request", "browser"]
NavigationMode = Literal["auto", "get", "google_get", "google_get_bypass"]
ErrorCategory = Literal[
    "timeout", "challenge_block", "navigation_error", "metadata_error"
]

_TRACKER_URL_PATTERNS: list[str] = [
    "*google-analytics.com*",
    "*googletagmanager.com*",
    "*facebook.net*",
    "*doubleclick.net*",
    "*sentry.io*",
    "*hotjar.com*",
    "*clarity.ms*",
    "*datadoghq-browser-agent.com*",
    "*segment.io*",
    "*analytics.js*",
    "*.woff",
    "*.woff2",
    "*.ttf",
]


class ScrapeRequest(BaseModel):
    url: HttpUrl
    execution_mode: ExecutionMode = "auto"
    navigation_mode: NavigationMode = "auto"
    max_retries: int = Field(default=2, ge=0, le=3)
    wait_for_selector: Optional[str] = None
    wait_timeout_seconds: int = Field(
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        ge=1,
        le=DEFAULT_SCRAPE_TIMEOUT_SECONDS,
    )
    block_images: bool = True
    block_images_and_css: bool = False
    block_trackers: bool = True
    wait_for_complete_page_load: bool = True
    user_agent: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    cookies: Optional[dict[str, str]] = None
    window_size: Optional[list[int]] = None
    lang: Optional[str] = None
    headless: bool = False
    proxy: Optional[str] = None

    @field_validator("window_size")
    @classmethod
    def validate_window_size(cls, value: Optional[list[int]]) -> Optional[list[int]]:
        if value is None:
            return value
        if len(value) != 2:
            raise ValueError("window_size must have exactly 2 integers")
        return value


class ScrapeResponse(BaseModel):
    url: str
    final_url: Optional[str]
    status_code: Optional[int]
    headers: Optional[dict[str, str]]
    html: str
    error: Optional[str]
    metadata_error: Optional[str] = None
    request_id: str
    attempts: int
    strategy_used: Optional[str]
    render_ms: int
    blocked_detected: bool
    challenge_detected: bool
    error_category: Optional[ErrorCategory] = None
    execution_tier: Optional[str] = None
    detected_challenge: Optional[str] = None


def make_error_payload(
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
    return {
        "url": url,
        "final_url": None,
        "status_code": None,
        "headers": None,
        "html": "",
        "error": message,
        "metadata_error": None,
        "request_id": request_id,
        "attempts": attempts,
        "strategy_used": strategy_used,
        "render_ms": render_ms,
        "blocked_detected": False,
        "challenge_detected": False,
        "error_category": error_category,
        "execution_tier": execution_tier,
        "detected_challenge": detected_challenge,
    }


def make_validation_error_payload(url: str, message: str) -> dict[str, Any]:
    return make_error_payload(
        url,
        message,
        request_id=str(uuid.uuid4()),
        attempts=0,
        strategy_used=None,
        render_ms=0,
        error_category="navigation_error",
    )


class ScraperEngine:
    """Deep module orchestrating anti-detect HTTP and browser execution tiers."""

    def __init__(self, runtime_root: Path = _RUNTIME_ROOT) -> None:
        self.runtime_root = runtime_root
        self._active_request_ids: set[str] = set()
        self._active_request_ids_lock = threading.Lock()

    def register_request_id(self, request_id: str) -> None:
        with self._active_request_ids_lock:
            if request_id in self._active_request_ids:
                raise RuntimeError("request id collision detected")
            self._active_request_ids.add(request_id)

    def unregister_request_id(self, request_id: str) -> None:
        with self._active_request_ids_lock:
            self._active_request_ids.discard(request_id)

    @classmethod
    def resolve_strategies(
        cls, mode: NavigationMode, max_retries: int
    ) -> list[str]:
        max_attempts = 1 + max_retries
        if mode == "auto":
            ordered = ["google_get", "google_get_bypass", "get"]
            return ordered[: min(len(ordered), max_attempts)]
        return [mode] * max_attempts

    @classmethod
    def navigate(
        cls, driver: Driver, target_url: str, strategy: str, timeout_seconds: int
    ) -> None:
        if strategy == "google_get_bypass":
            try:
                driver.google_get(
                    target_url, bypass_cloudflare=True, timeout=timeout_seconds
                )
                return
            except TypeError:
                driver.google_get(target_url, bypass_cloudflare=True)
                return

        if strategy == "google_get":
            try:
                driver.google_get(target_url, timeout=timeout_seconds)
                return
            except TypeError:
                driver.google_get(target_url)
                return

        try:
            driver.get(target_url, timeout=timeout_seconds)
        except TypeError:
            driver.get(target_url)

    @classmethod
    def wait_for_readiness(
        cls,
        driver: Driver,
        *,
        selector: Optional[str],
        timeout_seconds: int,
    ) -> None:
        if selector:
            driver.wait_for_element(selector, wait=timeout_seconds)
            return
        driver.sleep(1)

    def run_request_tier(
        self,
        payload: ScrapeRequest,
        request_id: str,
        started_monotonic: float,
    ) -> Optional[dict[str, Any]]:
        target_url = str(payload.url)
        remaining_budget = max(
            1,
            int(
                DEFAULT_SCRAPE_TIMEOUT_SECONDS
                - (time.monotonic() - started_monotonic)
            ),
        )

        req_headers = dict(payload.headers) if payload.headers else {}
        user_agent = (
            payload.user_agent
            or req_headers.get("User-Agent")
            or req_headers.get("user-agent")
        )
        proxies = (
            {"http": payload.proxy, "https": payload.proxy}
            if payload.proxy
            else None
        )

        req = Request()
        try:
            resp = req.get(
                target_url,
                headers=req_headers if req_headers else None,
                cookies=payload.cookies,
                user_agent=user_agent,
                proxies=proxies,
                timeout=remaining_budget,
                browser="chrome",
                allow_redirects=True,
            )

            html = resp.text or ""
            status_code = int(resp.status_code) if resp.status_code is not None else 200
            headers_dict = (
                {str(k): str(v) for k, v in resp.headers.items()}
                if getattr(resp, "headers", None)
                else None
            )
            final_url = str(resp.url) if getattr(resp, "url", None) else target_url

            assessment = ChallengeDetector.detect(html, status_code)
            render_ms = int((time.monotonic() - started_monotonic) * 1000)

            is_clean_success = (
                assessment.is_clean
                and (200 <= status_code < 300)
                and len(html.strip()) > 0
                and not payload.wait_for_selector
            )

            if payload.execution_mode == "auto" and not is_clean_success:
                logger.info(
                    "request_tier_escalating request_id=%s host=%s status=%d blocked=%s challenge=%s",
                    request_id,
                    urlparse(target_url).hostname,
                    status_code,
                    assessment.blocked_detected,
                    assessment.challenge_detected,
                )
                return None

            result = {
                "url": target_url,
                "final_url": final_url,
                "status_code": status_code,
                "headers": headers_dict,
                "html": html,
                "error": None,
                "metadata_error": None,
                "request_id": request_id,
                "attempts": 1,
                "strategy_used": "anti_detect_request",
                "render_ms": render_ms,
                "blocked_detected": assessment.blocked_detected,
                "challenge_detected": assessment.challenge_detected,
                "error_category": None,
                "execution_tier": "http_request",
                "detected_challenge": assessment.detected_marker,
            }

            if assessment.blocked_detected:
                result["error"] = "Challenge block detected"
                result["error_category"] = "challenge_block"

            return result
        finally:
            try:
                req.close()
            except Exception:
                pass

    def run_browser_tier(
        self,
        payload: ScrapeRequest,
        request_id: str,
        started_monotonic: float,
    ) -> dict[str, Any]:
        target_url = str(payload.url)
        runtime_dir = self.runtime_root / request_id
        profile_dir = runtime_dir / "profile"
        driver: Optional[Driver] = None

        runtime_dir.mkdir(parents=True, exist_ok=False)
        profile_dir.mkdir(parents=True, exist_ok=False)

        strategies = self.resolve_strategies(
            payload.navigation_mode, payload.max_retries
        )
        attempts = 0

        user_agent = payload.user_agent
        if not user_agent and payload.headers:
            user_agent = payload.headers.get("User-Agent") or payload.headers.get(
                "user-agent"
            )

        try:
            driver = Driver(
                headless=payload.headless,
                enable_xvfb_virtual_display=not payload.headless,
                proxy=payload.proxy,
                profile=str(profile_dir),
                tiny_profile=True,
                block_images=payload.block_images,
                block_images_and_css=payload.block_images_and_css,
                wait_for_complete_page_load=payload.wait_for_complete_page_load,
                user_agent=user_agent,
                window_size=payload.window_size,
                lang=payload.lang,
                remove_default_browser_check_argument=True,
            )

            if payload.block_trackers and hasattr(driver, "_tab"):
                try:
                    driver._tab.block_urls(_TRACKER_URL_PATTERNS)
                except Exception:
                    pass

            if payload.cookies:
                try:
                    for c_name, c_val in payload.cookies.items():
                        try:
                            driver.add_cookies(
                                [
                                    {
                                        "name": str(c_name),
                                        "value": str(c_val),
                                        "url": target_url,
                                    }
                                ]
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

            if payload.headers and hasattr(driver, "_tab"):
                try:
                    driver._tab.set_extra_http_headers(payload.headers)
                except Exception:
                    pass

            for attempt_index, strategy in enumerate(strategies, start=1):
                attempts = attempt_index
                try:
                    remaining_budget = max(
                        1,
                        int(
                            DEFAULT_SCRAPE_TIMEOUT_SECONDS
                            - (time.monotonic() - started_monotonic)
                        ),
                    )
                    self.navigate(driver, target_url, strategy, remaining_budget)
                    self.wait_for_readiness(
                        driver,
                        selector=payload.wait_for_selector,
                        timeout_seconds=min(
                            payload.wait_timeout_seconds, remaining_budget
                        ),
                    )

                    html = driver.page_html or ""
                    meta = MetadataExtractor.fetch(driver, target_url)
                    assessment = ChallengeDetector.detect(html, meta.status_code)

                    if assessment.challenge_detected or assessment.blocked_detected:
                        logger.warning(
                            "scrape_challenge_detected request_id=%s host=%s strategy=%s attempt=%d marker=%s",
                            request_id,
                            urlparse(target_url).hostname,
                            strategy,
                            attempt_index,
                            assessment.detected_marker,
                        )
                        if attempt_index < len(strategies):
                            continue

                        render_ms = int((time.monotonic() - started_monotonic) * 1000)
                        return {
                            "url": target_url,
                            "final_url": meta.final_url,
                            "status_code": meta.status_code,
                            "headers": meta.headers,
                            "html": html,
                            "error": f"Bot challenge detected ({assessment.detected_marker or 'unknown'})",
                            "metadata_error": meta.metadata_error,
                            "request_id": request_id,
                            "attempts": attempts,
                            "strategy_used": strategy,
                            "render_ms": render_ms,
                            "blocked_detected": assessment.blocked_detected,
                            "challenge_detected": assessment.challenge_detected,
                            "error_category": "challenge_block",
                            "execution_tier": "browser_driver",
                            "detected_challenge": assessment.detected_marker,
                        }

                    render_ms = int((time.monotonic() - started_monotonic) * 1000)
                    return {
                        "url": target_url,
                        "final_url": meta.final_url,
                        "status_code": meta.status_code,
                        "headers": meta.headers,
                        "html": html,
                        "error": None,
                        "metadata_error": meta.metadata_error,
                        "request_id": request_id,
                        "attempts": attempts,
                        "strategy_used": strategy,
                        "render_ms": render_ms,
                        "blocked_detected": False,
                        "challenge_detected": False,
                        "error_category": None,
                        "execution_tier": "browser_driver",
                        "detected_challenge": None,
                    }
                except Exception as exc:
                    logger.warning(
                        "scrape_attempt_failed request_id=%s host=%s mode=%s strategy=%s attempt=%d error=%s",
                        request_id,
                        urlparse(target_url).hostname,
                        payload.navigation_mode,
                        strategy,
                        attempt_index,
                        str(exc),
                    )
                    if attempt_index < len(strategies):
                        continue

                    render_ms = int((time.monotonic() - started_monotonic) * 1000)
                    category: ErrorCategory = (
                        "timeout" if "timeout" in str(exc).lower() else "navigation_error"
                    )
                    return make_error_payload(
                        target_url,
                        str(exc),
                        request_id=request_id,
                        attempts=attempts,
                        strategy_used=strategy,
                        render_ms=render_ms,
                        error_category=category,
                        execution_tier="browser_driver",
                    )

            render_ms = int((time.monotonic() - started_monotonic) * 1000)
            return make_error_payload(
                target_url,
                "Scrape failed after all strategy attempts",
                request_id=request_id,
                attempts=attempts,
                strategy_used=strategies[-1] if strategies else None,
                render_ms=render_ms,
                error_category="navigation_error",
                execution_tier="browser_driver",
            )
        finally:
            try:
                if driver is not None:
                    driver.close()
            finally:
                shutil.rmtree(runtime_dir, ignore_errors=True)

    def execute(
        self, payload: ScrapeRequest, deadline_monotonic: Optional[float] = None
    ) -> dict[str, Any]:
        target_url = str(payload.url)
        request_id = str(uuid.uuid4())
        started_monotonic = time.monotonic()

        if deadline_monotonic and started_monotonic >= deadline_monotonic:
            return make_error_payload(
                target_url,
                "Scrape timed out in threadpool queue before execution started",
                request_id=request_id,
                attempts=0,
                strategy_used=None,
                render_ms=0,
                error_category="timeout",
            )

        self.register_request_id(request_id)
        try:
            should_try_request_tier = (
                payload.execution_mode == "request"
                or (
                    payload.execution_mode == "auto"
                    and payload.navigation_mode == "auto"
                    and not payload.wait_for_selector
                )
            )

            if should_try_request_tier:
                try:
                    request_result = self.run_request_tier(
                        payload, request_id, started_monotonic
                    )
                    if request_result is not None:
                        return request_result
                except Exception as exc:
                    logger.info(
                        "request_tier_failed request_id=%s host=%s error=%s",
                        request_id,
                        urlparse(target_url).hostname,
                        str(exc),
                    )
                    if payload.execution_mode == "request":
                        render_ms = int((time.monotonic() - started_monotonic) * 1000)
                        return make_error_payload(
                            target_url,
                            str(exc),
                            request_id=request_id,
                            attempts=1,
                            strategy_used="anti_detect_request",
                            render_ms=render_ms,
                            error_category="navigation_error",
                            execution_tier="http_request",
                        )

            return self.run_browser_tier(payload, request_id, started_monotonic)
        finally:
            self.unregister_request_id(request_id)

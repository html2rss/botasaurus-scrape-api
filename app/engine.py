# app/engine.py
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from botasaurus.browser import Driver
from botasaurus.request import Request
from pydantic import BaseModel, Field, HttpUrl, ValidationInfo, field_validator

from app.detector import ChallengeDetector
from app.metadata import MetadataExtractor
from app.xhr_collector import XhrCollector

logger = logging.getLogger("botasaurus_scrape_api")

DEFAULT_SCRAPE_TIMEOUT_SECONDS = int(os.getenv("SCRAPE_TIMEOUT_SECONDS", "20"))
DEFAULT_WAIT_TIMEOUT_SECONDS = min(15, DEFAULT_SCRAPE_TIMEOUT_SECONDS)
_RUNTIME_ROOT = Path("/tmp/scrape")

ExecutionMode = Literal["auto", "request", "browser"]
NavigationMode = Literal[
    "auto", "get", "google_get", "google_get_bypass", "organic_get"
]
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
    wait_for_selector: str | None = None
    wait_timeout_seconds: int = Field(
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        description=(
            "Selector wait timeout in seconds. Values outside "
            f"[1, {DEFAULT_SCRAPE_TIMEOUT_SECONDS}] are clamped into that "
            "range so scrape still runs; they are not rejected with 422."
        ),
    )
    scroll: bool = False
    scroll_to_bottom: bool = False
    block_images: bool = True
    block_images_and_css: bool = False
    block_trackers: bool = True
    wait_for_complete_page_load: bool = True
    user_agent: str | None = None
    headers: dict[str, str] | None = None
    cookies: dict[str, str] | None = None
    window_size: list[int] | None = Field(default=None, min_length=2, max_length=2)
    lang: str | None = None
    headless: bool = False
    proxy: str | None = None

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

    @field_validator("window_size")
    @classmethod
    def validate_window_size(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and len(value) != 2:
            raise ValueError("window_size must have exactly 2 integers")
        return value

    @property
    def should_scroll(self) -> bool:
        return self.scroll or self.scroll_to_bottom

    @property
    def effective_user_agent(self) -> str | None:
        if self.user_agent:
            return self.user_agent
        if self.headers:
            return self.headers.get("User-Agent") or self.headers.get("user-agent")
        return None


HTML_DOCUMENT_CONTENT_TYPE = "text/html; charset=utf-8"


def utf8_normalize_html(html: str) -> str:
    if not html:
        return html
    if not isinstance(html, str):
        html = str(html)
    try:
        html = html.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):  # fmt: skip
        pass
    return html.encode("utf-8", errors="replace").decode("utf-8")


def html_document_headers(
    html: str, headers: dict[str, str] | None
) -> tuple[str, dict[str, str] | None]:
    if not html:
        return html, headers
    normalized = utf8_normalize_html(html)
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if str(key).lower() == "content-type":
            continue
        out[str(key)] = str(value)
    out["content-type"] = HTML_DOCUMENT_CONTENT_TYPE
    return normalized, out


class XhrResponse(BaseModel):
    url: str
    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: str


class ScrapeResponse(BaseModel):
    url: str
    final_url: str | None = None
    status_code: int | None = None
    headers: dict[str, str] | None = None
    html: str = ""
    error: str | None = None
    metadata_error: str | None = None
    request_id: str
    attempts: int = 0
    strategy_used: str | None = None
    render_ms: int = 0
    blocked_detected: bool = False
    challenge_detected: bool = False
    error_category: ErrorCategory | None = None
    execution_tier: str | None = None
    detected_challenge: str | None = None
    xhr_responses: list[XhrResponse] = Field(default_factory=list)

    @classmethod
    def create_success(
        cls,
        url: str,
        *,
        request_id: str,
        html: str,
        attempts: int,
        strategy_used: str,
        render_ms: int,
        execution_tier: str,
        final_url: str | None = None,
        status_code: int | None = 200,
        headers: dict[str, str] | None = None,
        metadata_error: str | None = None,
        blocked_detected: bool = False,
        challenge_detected: bool = False,
        error: str | None = None,
        error_category: ErrorCategory | None = None,
        detected_challenge: str | None = None,
        xhr_responses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        html, headers = html_document_headers(html, headers)
        return cls(
            url=url,
            final_url=final_url or url,
            status_code=status_code,
            headers=headers,
            html=html,
            error=error,
            metadata_error=metadata_error,
            request_id=request_id,
            attempts=attempts,
            strategy_used=strategy_used,
            render_ms=render_ms,
            blocked_detected=blocked_detected,
            challenge_detected=challenge_detected,
            error_category=error_category,
            execution_tier=execution_tier,
            detected_challenge=detected_challenge,
            xhr_responses=xhr_responses or [],
        ).model_dump()

    @classmethod
    def create_error(
        cls,
        url: str,
        error: str,
        *,
        request_id: str,
        attempts: int = 0,
        strategy_used: str | None = None,
        render_ms: int = 0,
        error_category: ErrorCategory | None = None,
        execution_tier: str | None = None,
        detected_challenge: str | None = None,
        blocked_detected: bool = False,
        challenge_detected: bool = False,
        html: str = "",
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        final_url: str | None = None,
        metadata_error: str | None = None,
        xhr_responses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        html, headers = html_document_headers(html, headers)
        return cls(
            url=url,
            final_url=final_url,
            status_code=status_code,
            headers=headers,
            html=html,
            error=error,
            metadata_error=metadata_error,
            request_id=request_id,
            attempts=attempts,
            strategy_used=strategy_used,
            render_ms=render_ms,
            blocked_detected=blocked_detected,
            challenge_detected=challenge_detected,
            error_category=error_category,
            execution_tier=execution_tier,
            detected_challenge=detected_challenge,
            xhr_responses=xhr_responses or [],
        ).model_dump()


def make_error_payload(
    url: str,
    message: str,
    *,
    request_id: str,
    attempts: int = 0,
    strategy_used: str | None = None,
    render_ms: int = 0,
    error_category: ErrorCategory | None = None,
    execution_tier: str | None = None,
    detected_challenge: str | None = None,
) -> dict[str, Any]:
    return ScrapeResponse.create_error(
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


def make_validation_error_payload(url: str, message: str) -> dict[str, Any]:
    return ScrapeResponse.create_error(
        url,
        message,
        request_id=str(uuid.uuid4()),
        attempts=0,
        strategy_used=None,
        render_ms=0,
        error_category="navigation_error",
    )


class ScrapeSession:
    """Encapsulates per-request concurrency registration and filesystem isolation."""

    def __init__(self, engine: ScraperEngine, request_id: str) -> None:
        self.engine = engine
        self.request_id = request_id
        self.runtime_dir = engine.runtime_root / request_id
        self.profile_dir = self.runtime_dir / "profile"
        self.driver: Driver | None = None

    def __enter__(self) -> ScrapeSession:
        self.engine.register_request_id(self.request_id)
        return self

    def prepare_profile_dirs(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=False)
        self.profile_dir.mkdir(parents=True, exist_ok=False)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if self.driver is not None:
                try:
                    self.driver.close()
                except Exception:
                    # Best-effort driver shutdown during cleanup
                    pass
        finally:
            shutil.rmtree(self.runtime_dir, ignore_errors=True)
            self.engine.unregister_request_id(self.request_id)


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
    def resolve_strategies(cls, mode: NavigationMode, max_retries: int) -> list[str]:
        max_attempts = 1 + max_retries
        if mode == "auto":
            ordered = ["google_get", "google_get_bypass", "get"]
            return ordered[: min(len(ordered), max_attempts)]
        return [mode] * max_attempts

    @classmethod
    def navigate(
        cls, driver: Driver, target_url: str, strategy: str, timeout_seconds: int
    ) -> None:
        if strategy == "organic_get":
            method = getattr(
                driver, "organic_get", getattr(driver, "google_get", driver.get)
            )
        elif strategy.startswith("google_get"):
            method = getattr(driver, "google_get", driver.get)
        else:
            method = driver.get

        kwargs: dict[str, Any] = {}
        if strategy == "google_get_bypass":
            kwargs["bypass_cloudflare"] = True
        try:
            method(target_url, timeout=timeout_seconds, **kwargs)
        except TypeError:
            method(target_url, **kwargs)

    @classmethod
    def _configure_driver(
        cls,
        driver: Driver,
        payload: ScrapeRequest,
        target_url: str,
        collector: XhrCollector | None = None,
    ) -> None:
        if hasattr(driver, "_tab"):
            if collector is not None:
                try:
                    collector.install(driver._tab)
                except Exception:
                    # Best-effort XHR capture; HTML scrape must still proceed
                    pass

            if payload.block_trackers:
                try:
                    driver._tab.block_urls(_TRACKER_URL_PATTERNS)
                except Exception:
                    # Optional CDP URL blocker feature
                    pass

        if payload.cookies:
            for c_name, c_val in payload.cookies.items():
                try:
                    driver.add_cookies(
                        [{"name": str(c_name), "value": str(c_val), "url": target_url}]
                    )
                except Exception:
                    # Best-effort cookie initialization
                    pass

        if payload.headers and hasattr(driver, "_tab"):
            try:
                driver._tab.set_extra_http_headers(payload.headers)
            except Exception:
                # Optional CDP extra HTTP headers feature
                pass

    @classmethod
    def wait_for_readiness(
        cls,
        driver: Driver,
        *,
        selector: str | None,
        timeout_seconds: int,
    ) -> None:
        if selector:
            driver.wait_for_element(selector, wait=timeout_seconds)
            return

        sleep_random_fn = getattr(driver, "sleep_random", None)
        if callable(sleep_random_fn):
            try:
                sleep_random_fn(0.5, 1.2)
                return
            except Exception:
                # Fall back to standard sleep if driver sleep_random fails
                pass
        driver.sleep(1)

    @classmethod
    def apply_scrolling(cls, driver: Driver) -> None:
        scroll_bottom_fn = getattr(driver, "scroll_to_bottom", None)
        scroll_fn = getattr(driver, "scroll", None)
        run_js_fn = getattr(driver, "run_js", None)

        if callable(scroll_bottom_fn):
            try:
                scroll_bottom_fn()
            except Exception:
                # Fall back to alternative scrolling if scroll_to_bottom fails
                pass
        elif callable(scroll_fn):
            try:
                scroll_fn()
            except Exception:
                # Fall back to JS scroll if scroll fails
                pass
        elif callable(run_js_fn):
            try:
                run_js_fn("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                # Fall back to execute_script if run_js fails
                pass
        elif hasattr(driver, "execute_script"):
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                # Best-effort JS scroll execution
                pass

        sleep_random_fn = getattr(driver, "sleep_random", None)
        if callable(sleep_random_fn):
            try:
                sleep_random_fn(0.4, 0.9)
                return
            except Exception:
                # Fall back to standard sleep if sleep_random fails
                pass

        try:
            driver.sleep(0.5)
        except Exception:
            # Best-effort post-scroll timing wait
            pass

    @staticmethod
    def _harvest_xhr(collector: XhrCollector, driver: Driver) -> list[dict[str, Any]]:
        tab = getattr(driver, "_tab", None)
        if tab is None:
            return collector.results()
        try:
            return collector.harvest(tab)
        except Exception as exc:
            logger.debug("xhr_harvest_failed error=%s", str(exc))
            return collector.results()

    def run_request_tier(
        self,
        payload: ScrapeRequest,
        request_id: str,
        started_monotonic: float,
    ) -> dict[str, Any] | None:
        target_url = str(payload.url)
        remaining_budget = max(
            1,
            int(
                DEFAULT_SCRAPE_TIMEOUT_SECONDS - (time.monotonic() - started_monotonic)
            ),
        )

        req_headers = dict(payload.headers) if payload.headers else {}
        proxies = (
            {"http": payload.proxy, "https": payload.proxy} if payload.proxy else None
        )

        req = Request()
        try:
            resp = req.get(
                target_url,
                headers=req_headers if req_headers else None,
                cookies=payload.cookies,
                user_agent=payload.effective_user_agent,
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
                and not payload.should_scroll
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

            error_msg = (
                "Challenge block detected" if assessment.blocked_detected else None
            )
            error_cat: ErrorCategory | None = (
                "challenge_block" if assessment.blocked_detected else None
            )

            return ScrapeResponse.create_success(
                target_url,
                request_id=request_id,
                html=html,
                final_url=final_url,
                status_code=status_code,
                headers=headers_dict,
                attempts=1,
                strategy_used="anti_detect_request",
                render_ms=render_ms,
                blocked_detected=assessment.blocked_detected,
                challenge_detected=assessment.challenge_detected,
                error=error_msg,
                error_category=error_cat,
                execution_tier="http_request",
                detected_challenge=assessment.detected_marker,
            )
        finally:
            try:
                req.close()
            except Exception:
                # Best-effort HTTP client cleanup
                pass

    def run_browser_tier(
        self,
        payload: ScrapeRequest,
        session: ScrapeSession,
        started_monotonic: float,
    ) -> dict[str, Any]:
        target_url = str(payload.url)
        request_id = session.request_id
        session.prepare_profile_dirs()

        strategies = self.resolve_strategies(
            payload.navigation_mode, payload.max_retries
        )
        attempts = 0
        collector = XhrCollector(target_url)

        session.driver = Driver(
            headless=payload.headless,
            enable_xvfb_virtual_display=not payload.headless,
            proxy=payload.proxy,
            profile=str(session.profile_dir),
            tiny_profile=True,
            block_images=payload.block_images,
            block_images_and_css=payload.block_images_and_css,
            wait_for_complete_page_load=payload.wait_for_complete_page_load,
            user_agent=payload.effective_user_agent,
            window_size=payload.window_size,
            lang=payload.lang,
            remove_default_browser_check_argument=True,
        )
        self._configure_driver(session.driver, payload, target_url, collector=collector)

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
                self.navigate(session.driver, target_url, strategy, remaining_budget)
                self.wait_for_readiness(
                    session.driver,
                    selector=payload.wait_for_selector,
                    timeout_seconds=min(payload.wait_timeout_seconds, remaining_budget),
                )

                if payload.should_scroll:
                    self.apply_scrolling(session.driver)

                xhr_responses = self._harvest_xhr(collector, session.driver)

                html = session.driver.page_html or ""
                meta = MetadataExtractor.fetch(session.driver, target_url)
                assessment = ChallengeDetector.detect(
                    html, meta.status_code, driver=session.driver
                )

                if assessment.challenge_detected or assessment.blocked_detected:
                    bypass_fn = getattr(session.driver, "bypass_cloudflare", None)
                    if callable(bypass_fn):
                        try:
                            bypass_fn()
                            html = session.driver.page_html or ""
                            meta = MetadataExtractor.fetch(session.driver, target_url)
                            assessment = ChallengeDetector.detect(
                                html, meta.status_code, driver=session.driver
                            )
                            xhr_responses = self._harvest_xhr(collector, session.driver)
                        except Exception as exc:
                            logger.debug(
                                "bypass_cloudflare_attempt_failed error=%s", str(exc)
                            )

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
                        # Drop interstitial JSON from the failed attempt so it
                        # cannot pollute the next strategy's xhr_responses/cap.
                        collector.reset()
                        continue

                    render_ms = int((time.monotonic() - started_monotonic) * 1000)
                    return ScrapeResponse.create_error(
                        target_url,
                        f"Bot challenge detected ({assessment.detected_marker or 'unknown'})",
                        request_id=request_id,
                        html=html,
                        final_url=meta.final_url,
                        status_code=meta.status_code,
                        headers=meta.headers,
                        metadata_error=meta.metadata_error,
                        attempts=attempts,
                        strategy_used=strategy,
                        render_ms=render_ms,
                        blocked_detected=assessment.blocked_detected,
                        challenge_detected=assessment.challenge_detected,
                        error_category="challenge_block",
                        execution_tier="browser_driver",
                        detected_challenge=assessment.detected_marker,
                        xhr_responses=xhr_responses,
                    )

                render_ms = int((time.monotonic() - started_monotonic) * 1000)
                return ScrapeResponse.create_success(
                    target_url,
                    request_id=request_id,
                    html=html,
                    final_url=meta.final_url,
                    status_code=meta.status_code,
                    headers=meta.headers,
                    metadata_error=meta.metadata_error,
                    attempts=attempts,
                    strategy_used=strategy,
                    render_ms=render_ms,
                    execution_tier="browser_driver",
                    xhr_responses=xhr_responses,
                )
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
                    collector.reset()
                    continue

                render_ms = int((time.monotonic() - started_monotonic) * 1000)
                category: ErrorCategory = (
                    "timeout" if "timeout" in str(exc).lower() else "navigation_error"
                )
                return ScrapeResponse.create_error(
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
        return ScrapeResponse.create_error(
            target_url,
            "Scrape failed after all strategy attempts",
            request_id=request_id,
            attempts=attempts,
            strategy_used=strategies[-1] if strategies else None,
            render_ms=render_ms,
            error_category="navigation_error",
            execution_tier="browser_driver",
        )

    def execute(
        self, payload: ScrapeRequest, deadline_monotonic: float | None = None
    ) -> dict[str, Any]:
        target_url = str(payload.url)
        request_id = str(uuid.uuid4())
        started_monotonic = time.monotonic()

        if deadline_monotonic and started_monotonic >= deadline_monotonic:
            return ScrapeResponse.create_error(
                target_url,
                "Scrape timed out in threadpool queue before execution started",
                request_id=request_id,
                attempts=0,
                strategy_used=None,
                render_ms=0,
                error_category="timeout",
            )

        with ScrapeSession(self, request_id) as session:
            should_try_request_tier = payload.execution_mode == "request" or (
                payload.execution_mode == "auto"
                and payload.navigation_mode == "auto"
                and not payload.wait_for_selector
                and not payload.should_scroll
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
                        return ScrapeResponse.create_error(
                            target_url,
                            str(exc),
                            request_id=request_id,
                            attempts=1,
                            strategy_used="anti_detect_request",
                            render_ms=render_ms,
                            error_category="navigation_error",
                            execution_tier="http_request",
                        )

            return self.run_browser_tier(payload, session, started_monotonic)

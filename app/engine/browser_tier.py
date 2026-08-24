"""Chromium browser execution tier."""

from __future__ import annotations

import errno
import logging
import time
from urllib.parse import urlparse

from botasaurus.browser import Driver

from app.config import Settings
from app.engine.driver_capabilities import call_if_available
from app.engine.envelope import build_error, build_success
from app.engine.request_tier import remaining_total_seconds
from app.engine.session import ScrapeSession
from app.engine.strategies import (
    apply_scrolling,
    configure_driver,
    harvest_xhr,
    navigate,
    resolve_strategies,
    wait_for_readiness,
)
from app.infra.detector import ChallengeDetector
from app.infra.metadata import MetadataExtractor
from app.infra.scrape_progress import ScrapeProgress
from app.infra.xhr_collector import XhrCollector
from app.schemas.enums import ErrorCategory, ExecutionTier, TimeoutPhase
from app.schemas.request import ScrapeRequest
from app.schemas.response import ScrapeError, ScrapeSuccess

logger = logging.getLogger("botasaurus_scrape_api")


def remaining_work_seconds(settings: Settings, browser_ready_monotonic: float) -> int:
    return max(
        1,
        int(
            settings.scrape_work_timeout_seconds
            - (time.monotonic() - browser_ready_monotonic)
        ),
    )


def browser_step_budget_seconds(
    settings: Settings, started_monotonic: float, browser_ready_monotonic: float
) -> int:
    return min(
        remaining_total_seconds(settings, started_monotonic),
        remaining_work_seconds(settings, browser_ready_monotonic),
    )


def is_timeout_exception(exc: Exception) -> bool:
    return "timeout" in str(exc).lower()


def run_browser_tier(
    payload: ScrapeRequest,
    session: ScrapeSession,
    started_monotonic: float,
    progress: ScrapeProgress,
    *,
    settings: Settings,
) -> ScrapeSuccess | ScrapeError:
    target_url = str(payload.url)
    request_id = session.request_id
    progress.mark(
        TimeoutPhase.BOOT,
        execution_tier=ExecutionTier.BROWSER_DRIVER,
    )
    try:
        session.prepare_profile_dirs()
    except OSError as exc:
        render_ms = int((time.monotonic() - started_monotonic) * 1000)
        if exc.errno == errno.ENOSPC:
            detail = "Scrape runtime storage full"
        else:
            detail = "Scrape runtime storage unavailable"
        return build_error(
            target_url,
            f"{detail}: {exc}",
            request_id=request_id,
            attempts=0,
            render_ms=render_ms,
            error_category=ErrorCategory.NAVIGATION_ERROR,
            execution_tier=ExecutionTier.BROWSER_DRIVER,
            timeout_phase=TimeoutPhase.BOOT,
        )

    strategies = resolve_strategies(payload.navigation_mode, payload.max_retries)
    attempts = 0
    collector = XhrCollector(target_url)
    driver_window_size = (
        [payload.window_size.width, payload.window_size.height]
        if payload.window_size
        else None
    )

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
        window_size=driver_window_size,
        lang=payload.lang,
        remove_default_browser_check_argument=True,
    )
    configure_driver(session.driver, payload, target_url, collector=collector)
    browser_ready_monotonic = time.monotonic()
    progress.mark(
        TimeoutPhase.WORK,
        execution_tier=ExecutionTier.BROWSER_DRIVER,
    )

    for attempt_index, strategy in enumerate(strategies, start=1):
        attempts = attempt_index
        progress.mark(
            TimeoutPhase.WORK,
            attempts=attempts,
            strategy_used=strategy,
            execution_tier=ExecutionTier.BROWSER_DRIVER,
        )
        try:
            step_budget = browser_step_budget_seconds(
                settings, started_monotonic, browser_ready_monotonic
            )
            navigate(session.driver, target_url, strategy, step_budget)
            wait_for_readiness(
                session.driver,
                selector=payload.wait_for_selector,
                timeout_seconds=min(payload.wait_timeout_seconds, step_budget),
            )

            if payload.scroll:
                apply_scrolling(session.driver)

            xhr_responses = harvest_xhr(collector, session.driver)

            html = session.driver.page_html or ""
            meta = MetadataExtractor.fetch(session.driver, target_url)
            assessment = ChallengeDetector.detect(
                html, meta.status_code, driver=session.driver
            )

            if assessment.challenge_detected or assessment.blocked_detected:
                call_if_available(session.driver, "bypass_cloudflare")
                html = session.driver.page_html or ""
                meta = MetadataExtractor.fetch(session.driver, target_url)
                assessment = ChallengeDetector.detect(
                    html, meta.status_code, driver=session.driver
                )
                xhr_responses = harvest_xhr(collector, session.driver)

            if assessment.challenge_detected or assessment.blocked_detected:
                logger.warning(
                    "scrape_challenge_detected request_id=%s host=%s strategy=%s attempt=%d marker=%s",
                    request_id,
                    urlparse(target_url).hostname,
                    strategy.value,
                    attempt_index,
                    assessment.detected_marker,
                )
                if attempt_index < len(strategies):
                    collector.reset()
                    continue

                render_ms = int((time.monotonic() - started_monotonic) * 1000)
                return build_error(
                    target_url,
                    f"Bot challenge detected ({assessment.detected_marker or 'unknown'})",
                    request_id=request_id,
                    error_category=ErrorCategory.CHALLENGE_BLOCK,
                    attempts=attempts,
                    strategy_used=strategy,
                    render_ms=render_ms,
                    execution_tier=ExecutionTier.BROWSER_DRIVER,
                    assessment=assessment,
                )

            render_ms = int((time.monotonic() - started_monotonic) * 1000)
            return build_success(
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
                execution_tier=ExecutionTier.BROWSER_DRIVER,
                assessment=assessment,
                xhr_responses=xhr_responses,
            )
        except Exception as exc:
            logger.warning(
                "scrape_attempt_failed request_id=%s host=%s mode=%s strategy=%s attempt=%d error=%s",
                request_id,
                urlparse(target_url).hostname,
                payload.navigation_mode,
                strategy.value,
                attempt_index,
                str(exc),
            )
            if attempt_index < len(strategies):
                collector.reset()
                continue

            render_ms = int((time.monotonic() - started_monotonic) * 1000)
            is_timeout = is_timeout_exception(exc)
            category = (
                ErrorCategory.TIMEOUT if is_timeout else ErrorCategory.NAVIGATION_ERROR
            )
            return build_error(
                target_url,
                str(exc),
                request_id=request_id,
                attempts=attempts,
                strategy_used=strategy,
                render_ms=render_ms,
                error_category=category,
                execution_tier=ExecutionTier.BROWSER_DRIVER,
                timeout_phase=TimeoutPhase.WORK if is_timeout else None,
            )

    render_ms = int((time.monotonic() - started_monotonic) * 1000)
    return build_error(
        target_url,
        "Scrape failed after all strategy attempts",
        request_id=request_id,
        attempts=attempts,
        strategy_used=strategies[-1] if strategies else None,
        render_ms=render_ms,
        error_category=ErrorCategory.NAVIGATION_ERROR,
        execution_tier=ExecutionTier.BROWSER_DRIVER,
    )

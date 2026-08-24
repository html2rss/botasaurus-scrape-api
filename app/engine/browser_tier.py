"""Chromium browser execution tier."""

from __future__ import annotations

import errno
import time
from typing import cast
from urllib.parse import urlparse

from app.config import Settings
from app.engine.budget import (
    browser_step_budget_seconds,
    elapsed_ms,
    is_timeout_exception,
)
from app.engine.driver_capabilities import DriverProtocol, call_if_available
from app.engine.envelope import build_error, build_success
from app.engine.session import ScrapeSession
from app.engine.strategies import (
    apply_scrolling,
    configure_driver,
    harvest_xhr,
    navigate,
    resolve_strategies,
    wait_for_readiness,
)
from app.infra.detector import ChallengeAssessment, ChallengeDetector
from app.infra.metadata import MetadataExtractor, MetadataResult
from app.infra.scrape_progress import ScrapeProgress
from app.infra.xhr_collector import XhrCollector
from app.logging_config import get_logger
from app.schemas.enums import ErrorCategory, ExecutionTier, TimeoutPhase
from app.schemas.request import ScrapeRequest
from app.schemas.response import ScrapeError, ScrapeSuccess, XhrResponse

logger = get_logger()


def _page_state(
    driver: DriverProtocol, target_url: str
) -> tuple[str, MetadataResult, ChallengeAssessment]:
    html = driver.page_html or ""
    meta = MetadataExtractor.fetch(driver, target_url)
    assessment = ChallengeDetector.detect(html, meta.status_code, driver=driver)
    return html, meta, assessment


def settle_page_state(
    driver: DriverProtocol,
    target_url: str,
    collector: XhrCollector,
) -> tuple[str, MetadataResult, ChallengeAssessment, list[XhrResponse]]:
    """Collect final page state, attempting one Cloudflare bypass on challenges.

    XHR harvest runs exactly once per attempt, after any bypass, so the
    consolidated pass captures post-bypass sub-resources.
    """
    html, meta, assessment = _page_state(driver, target_url)
    if assessment.is_clean:
        return html, meta, assessment, harvest_xhr(collector, driver)

    call_if_available(driver, "bypass_cloudflare")
    xhr_responses = harvest_xhr(collector, driver)
    html, meta, assessment = _page_state(driver, target_url)
    return html, meta, assessment, xhr_responses


def run_browser_tier(
    payload: ScrapeRequest,
    session: ScrapeSession,
    started_monotonic: float,
    progress: ScrapeProgress,
    *,
    settings: Settings,
) -> ScrapeSuccess | ScrapeError:
    from botasaurus.browser import Driver

    target_url = str(payload.url)
    request_id = session.request_id
    progress.mark(
        TimeoutPhase.BOOT,
        execution_tier=ExecutionTier.BROWSER_DRIVER,
    )
    try:
        session.prepare_profile_dirs()
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            detail = "Scrape runtime storage full"
        else:
            detail = "Scrape runtime storage unavailable"
        return build_error(
            target_url,
            f"{detail}: {exc}",
            request_id=request_id,
            attempts=0,
            render_ms=elapsed_ms(started_monotonic),
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

    try:
        driver = cast(
            DriverProtocol,
            Driver(
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
            ),
        )
    except Exception as exc:
        is_timeout = is_timeout_exception(exc)
        return build_error(
            target_url,
            str(exc),
            request_id=request_id,
            attempts=0,
            render_ms=elapsed_ms(started_monotonic),
            error_category=(
                ErrorCategory.TIMEOUT if is_timeout else ErrorCategory.NAVIGATION_ERROR
            ),
            execution_tier=ExecutionTier.BROWSER_DRIVER,
            timeout_phase=TimeoutPhase.BOOT,
        )

    session.driver = driver
    configure_driver(driver, payload, target_url, collector=collector)
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
            navigate(driver, target_url, strategy, step_budget)
            wait_for_readiness(
                driver,
                selector=payload.wait_for_selector,
                timeout_seconds=min(payload.wait_timeout_seconds, step_budget),
            )

            if payload.scroll:
                apply_scrolling(driver)

            html, meta, assessment, xhr_responses = settle_page_state(
                driver, target_url, collector
            )

            if not assessment.is_clean:
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

                return build_error(
                    target_url,
                    f"Bot challenge detected ({assessment.detected_marker or 'unknown'})",
                    request_id=request_id,
                    error_category=ErrorCategory.CHALLENGE_BLOCK,
                    attempts=attempts,
                    strategy_used=strategy,
                    render_ms=elapsed_ms(started_monotonic),
                    execution_tier=ExecutionTier.BROWSER_DRIVER,
                    assessment=assessment,
                )

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
                render_ms=elapsed_ms(started_monotonic),
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

            is_timeout = is_timeout_exception(exc)
            return build_error(
                target_url,
                str(exc),
                request_id=request_id,
                attempts=attempts,
                strategy_used=strategy,
                render_ms=elapsed_ms(started_monotonic),
                error_category=(
                    ErrorCategory.TIMEOUT
                    if is_timeout
                    else ErrorCategory.NAVIGATION_ERROR
                ),
                execution_tier=ExecutionTier.BROWSER_DRIVER,
                timeout_phase=TimeoutPhase.WORK if is_timeout else None,
            )

    return build_error(
        target_url,
        "Scrape failed after all strategy attempts",
        request_id=request_id,
        attempts=attempts,
        strategy_used=strategies[-1] if strategies else None,
        render_ms=elapsed_ms(started_monotonic),
        error_category=ErrorCategory.NAVIGATION_ERROR,
        execution_tier=ExecutionTier.BROWSER_DRIVER,
    )

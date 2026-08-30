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
from app.engine.envelope import TIMEOUT_ERROR_BY_PHASE, build_error, build_success
from app.engine.session import ScrapeSession
from app.engine.strategies import (
    apply_scrolling,
    configure_driver,
    harvest_xhr,
    navigate,
    resolve_strategies,
    wait_for_readiness,
)
from app.engine.warm_pool import DriverFingerprint
from app.engine.work_lease import WorkLease
from app.infra.detector import ChallengeAssessment, ChallengeDetector
from app.infra.metadata import MetadataExtractor, MetadataResult
from app.infra.xhr_collector import XhrCollector
from app.logging_config import get_logger
from app.schemas.enums import ErrorCategory, ExecutionTier, NavigationMode, TimeoutPhase
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


def _inspect_assessment(driver: DriverProtocol) -> ChallengeAssessment | None:
    """Best-effort challenge assessment after a nav/timeout exception.

    Lean path only: html + passive request status + driver signals via
    ChallengeDetector. Skips CDP log walks that can stall on a hung page.
    Metadata failures must not discard HTML/driver challenge signals.
    """
    try:
        html = driver.page_html or ""
    except Exception:
        return None
    try:
        status_code, _, _ = MetadataExtractor.extract_from_requests(driver)
    except Exception:
        status_code = None
    try:
        return ChallengeDetector.detect(html, status_code, driver=driver)
    except Exception:
        return None


def _challenge_block_error(
    target_url: str,
    *,
    request_id: str,
    attempts: int,
    strategy: NavigationMode | None,
    render_ms: int,
    assessment: ChallengeAssessment,
) -> ScrapeError:
    marker = assessment.detected_marker or "unknown"
    return build_error(
        target_url,
        f"Bot challenge detected ({marker})",
        request_id=request_id,
        error_category=ErrorCategory.CHALLENGE_BLOCK,
        attempts=attempts,
        strategy_used=strategy,
        render_ms=render_ms,
        execution_tier=ExecutionTier.BROWSER_DRIVER,
        assessment=assessment,
        timeout_phase=None,
    )


def _surface_unclean(
    target_url: str,
    *,
    request_id: str,
    attempts: int,
    strategy: NavigationMode,
    started_monotonic: float,
    assessment: ChallengeAssessment,
) -> ScrapeError:
    """Fail closed: any unclean assessment is challenge_block (no soft-retry)."""
    logger.warning(
        "scrape_challenge_detected request_id=%s host=%s strategy=%s "
        "attempt=%d marker=%s",
        request_id,
        urlparse(target_url).hostname,
        strategy.value,
        attempts,
        assessment.detected_marker,
    )
    return _challenge_block_error(
        target_url,
        request_id=request_id,
        attempts=attempts,
        strategy=strategy,
        render_ms=elapsed_ms(started_monotonic),
        assessment=assessment,
    )


_UNREADABLE_SURFACE = ChallengeAssessment(
    blocked_detected=True,
    challenge_detected=True,
    detected_marker="unreadable_surface",
)


def _boot_storage_error(
    target_url: str,
    request_id: str,
    started_monotonic: float,
    exc: OSError,
) -> ScrapeError:
    detail = (
        "Scrape runtime storage full"
        if exc.errno == errno.ENOSPC
        else "Scrape runtime storage unavailable"
    )
    return build_error(
        target_url,
        f"{detail}: {exc}",
        request_id=request_id,
        attempts=0,
        render_ms=elapsed_ms(started_monotonic),
        error_category=ErrorCategory.NAVIGATION_ERROR,
        execution_tier=ExecutionTier.BROWSER_DRIVER,
        timeout_phase=None,
    )


def _tier_exception_error(
    target_url: str,
    *,
    request_id: str,
    attempts: int,
    strategy: NavigationMode | None,
    started_monotonic: float,
    exc: Exception,
    timeout_phase: TimeoutPhase | None,
) -> ScrapeError:
    is_timeout = is_timeout_exception(exc)
    message = (
        TIMEOUT_ERROR_BY_PHASE[timeout_phase]
        if is_timeout and timeout_phase is not None
        else str(exc)
    )
    return build_error(
        target_url,
        message,
        request_id=request_id,
        attempts=attempts,
        strategy_used=strategy,
        render_ms=elapsed_ms(started_monotonic),
        error_category=(
            ErrorCategory.TIMEOUT if is_timeout else ErrorCategory.NAVIGATION_ERROR
        ),
        execution_tier=ExecutionTier.BROWSER_DRIVER,
        timeout_phase=timeout_phase if is_timeout else None,
    )


def _work_budget_timeout(
    target_url: str,
    *,
    request_id: str,
    attempts: int,
    strategy: NavigationMode | None,
    started_monotonic: float,
) -> ScrapeError:
    return build_error(
        target_url,
        TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.WORK],
        request_id=request_id,
        attempts=attempts,
        strategy_used=strategy,
        render_ms=elapsed_ms(started_monotonic),
        error_category=ErrorCategory.TIMEOUT,
        execution_tier=ExecutionTier.BROWSER_DRIVER,
        timeout_phase=TimeoutPhase.WORK,
    )


def run_browser_tier(
    payload: ScrapeRequest,
    session: ScrapeSession,
    started_monotonic: float,
    lease: WorkLease,
    *,
    settings: Settings,
) -> ScrapeSuccess | ScrapeError:
    from botasaurus.browser import Driver

    target_url = str(payload.url)
    request_id = session.request_id
    lease.mark(
        TimeoutPhase.BOOT,
        execution_tier=ExecutionTier.BROWSER_DRIVER,
    )
    fingerprint = DriverFingerprint.from_request(payload)
    session.warm_fingerprint = fingerprint
    warm_hit = False
    boot_started = time.monotonic()

    pool = session.engine.warm_pool
    taken = pool.take(fingerprint) if pool is not None else None
    if taken is not None:
        driver, spare_dir = taken
        # Assign immediately so session.__exit__ closes the spare on any raise.
        session.driver = driver
        session.profile_dir = spare_dir
        session.adopted_profile_dir = spare_dir
        try:
            session.prepare_runtime_dir()
        except OSError as exc:
            return _boot_storage_error(target_url, request_id, started_monotonic, exc)
        warm_hit = True
    else:
        try:
            session.prepare_profile_dirs()
        except OSError as exc:
            return _boot_storage_error(target_url, request_id, started_monotonic, exc)

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
            return _tier_exception_error(
                target_url,
                request_id=request_id,
                attempts=0,
                strategy=None,
                started_monotonic=started_monotonic,
                exc=exc,
                timeout_phase=TimeoutPhase.BOOT,
            )

        session.driver = driver

    session.warm_hit = warm_hit
    lease.set_warm_hit(warm_hit)
    boot_ms = int((time.monotonic() - boot_started) * 1000)
    logger.info(
        "scrape_boot request_id=%s warm_hit=%s boot_ms=%d",
        request_id,
        warm_hit,
        boot_ms,
    )

    strategies = resolve_strategies(payload.navigation_mode, payload.max_retries)
    attempts = 0
    collector = XhrCollector(target_url)
    driver = session.driver
    assert driver is not None

    configure_driver(driver, payload, target_url, collector=collector)
    browser_ready_monotonic = time.monotonic()
    lease.mark(
        TimeoutPhase.WORK,
        execution_tier=ExecutionTier.BROWSER_DRIVER,
    )

    for attempt_index, strategy in enumerate(strategies, start=1):
        attempts = attempt_index
        has_more = attempt_index < len(strategies)
        lease.mark(
            TimeoutPhase.WORK,
            attempts=attempts,
            strategy_used=strategy,
            execution_tier=ExecutionTier.BROWSER_DRIVER,
        )
        step_budget = browser_step_budget_seconds(
            settings, started_monotonic, browser_ready_monotonic
        )
        if step_budget <= 0:
            return _work_budget_timeout(
                target_url,
                request_id=request_id,
                attempts=attempts,
                strategy=strategy,
                started_monotonic=started_monotonic,
            )
        try:
            navigate(driver, target_url, strategy, step_budget)
            mid_wait = wait_for_readiness(
                driver,
                selector=payload.wait_for_selector,
                timeout_seconds=min(payload.wait_timeout_seconds, step_budget),
            )
            if mid_wait is not None:
                return _surface_unclean(
                    target_url,
                    request_id=request_id,
                    attempts=attempts,
                    strategy=strategy,
                    started_monotonic=started_monotonic,
                    assessment=mid_wait,
                )

            if payload.scroll:
                apply_scrolling(driver)

            html, meta, assessment, xhr_responses = settle_page_state(
                driver, target_url, collector
            )

            if not assessment.is_clean:
                return _surface_unclean(
                    target_url,
                    request_id=request_id,
                    attempts=attempts,
                    strategy=strategy,
                    started_monotonic=started_monotonic,
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
                "scrape_attempt_failed request_id=%s host=%s mode=%s strategy=%s "
                "attempt=%d error=%s",
                request_id,
                urlparse(target_url).hostname,
                payload.navigation_mode,
                strategy.value,
                attempt_index,
                str(exc),
            )
            assessment = _inspect_assessment(driver)
            if assessment is None or not assessment.is_clean:
                return _surface_unclean(
                    target_url,
                    request_id=request_id,
                    attempts=attempts,
                    strategy=strategy,
                    started_monotonic=started_monotonic,
                    assessment=assessment or _UNREADABLE_SURFACE,
                )
            if has_more:
                collector.reset()
                continue

            return _tier_exception_error(
                target_url,
                request_id=request_id,
                attempts=attempts,
                strategy=strategy,
                started_monotonic=started_monotonic,
                exc=exc,
                timeout_phase=(
                    TimeoutPhase.WORK if is_timeout_exception(exc) else None
                ),
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

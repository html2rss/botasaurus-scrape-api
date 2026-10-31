"""Chromium browser execution tier."""

import errno
import logging
import time
from urllib.parse import urlparse

from app.config import Settings
from app.engine.budget import (
    browser_step_budget_seconds,
    elapsed_ms,
    is_timeout_exception,
)
from app.engine.driver_protocol import DriverProtocol
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
from app.engine.warm_pool import DriverFingerprint, create_driver_from_fingerprint
from app.engine.work_lease import WorkLease
from app.infra.cookies import driver_cookies
from app.infra.detector import (
    UNREADABLE_SURFACE,
    ChallengeAssessment,
    ChallengeDetector,
)
from app.infra.metadata import MetadataResult, resolve_metadata
from app.infra.runtime_stats import home_is_writable, tmp_free_bytes
from app.infra.xhr_collector import XhrCollector
from app.logging_config import get_logger, log_event
from app.schemas.enums import ErrorCategory, ExecutionTier, NavigationMode, TimeoutPhase
from app.schemas.request import ScrapeRequest
from app.schemas.response import ScrapeError, ScrapeSuccess, XhrResponse

logger = get_logger()


def _log_browser_boot_failed(
    *,
    request_id: str,
    warm: bool,
    started_monotonic: float,
    error: object,
) -> None:
    log_event(
        logger,
        logging.WARNING,
        "browser_boot_failed",
        request_id=request_id,
        warm=warm,
        elapsed_ms=elapsed_ms(started_monotonic),
        error=str(error),
        home_writable=home_is_writable(),
        tmp_free_bytes=tmp_free_bytes(),
    )


def _page_state(
    driver: DriverProtocol, target_url: str, collector: XhrCollector
) -> tuple[str, MetadataResult, ChallengeAssessment]:
    html = driver.page_html or ""
    meta = resolve_metadata(collector.document, driver.current_url, target_url)
    assessment = ChallengeDetector.detect(
        html, meta.status_code, driver=driver, headers=meta.headers
    )
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
    html, meta, assessment = _page_state(driver, target_url, collector)
    if assessment.is_clean:
        return html, meta, assessment, harvest_xhr(collector, driver)

    driver.detect_and_bypass_cloudflare()
    xhr_responses = harvest_xhr(collector, driver)
    html, meta, assessment = _page_state(driver, target_url, collector)
    return html, meta, assessment, xhr_responses


def _inspect_assessment(
    driver: DriverProtocol, collector: XhrCollector
) -> ChallengeAssessment | None:
    """Best-effort challenge assessment after a nav/timeout exception.

    Lean path only: html + captured document response + driver signals.
    Skips anything that can stall on a hung page.
    """
    try:
        html = driver.page_html or ""
        document = collector.document
        return ChallengeDetector.detect(
            html,
            document.status_code if document else None,
            driver=driver,
            headers=document.headers if document else None,
        )
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
    log_event(
        logger,
        logging.WARNING,
        "scrape_challenge_detected",
        request_id=request_id,
        host=urlparse(target_url).hostname,
        strategy=strategy.value,
        attempt=attempts,
        marker=assessment.detected_marker,
    )
    return _challenge_block_error(
        target_url,
        request_id=request_id,
        attempts=attempts,
        strategy=strategy,
        render_ms=elapsed_ms(started_monotonic),
        assessment=assessment,
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
    target_url = str(payload.url)
    request_id = session.request_id
    lease.mark(
        TimeoutPhase.BOOT,
        execution_tier=ExecutionTier.BROWSER_DRIVER,
    )
    if lease.aborted:
        return build_error(
            target_url,
            TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.BOOT],
            request_id=request_id,
            error_category=ErrorCategory.TIMEOUT,
            attempts=0,
            render_ms=elapsed_ms(started_monotonic),
            execution_tier=ExecutionTier.BROWSER_DRIVER,
            timeout_phase=TimeoutPhase.BOOT,
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

        if lease.aborted:
            return build_error(
                target_url,
                TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.BOOT],
                request_id=request_id,
                error_category=ErrorCategory.TIMEOUT,
                attempts=0,
                render_ms=elapsed_ms(started_monotonic),
                execution_tier=ExecutionTier.BROWSER_DRIVER,
                timeout_phase=TimeoutPhase.BOOT,
            )

        try:
            driver = create_driver_from_fingerprint(fingerprint, session.profile_dir)
        except Exception as exc:
            _log_browser_boot_failed(
                request_id=request_id,
                warm=False,
                started_monotonic=started_monotonic,
                error=exc,
            )
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
    log_event(
        logger,
        logging.INFO,
        "scrape_boot",
        request_id=request_id,
        warm_hit=warm_hit,
        boot_ms=boot_ms,
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
                cookies=driver_cookies(driver, target_url, meta.final_url),
            )
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "scrape_attempt_failed",
                request_id=request_id,
                host=urlparse(target_url).hostname,
                mode=str(payload.navigation_mode),
                strategy=strategy.value,
                attempt=attempt_index,
                error=str(exc),
            )
            assessment = _inspect_assessment(driver, collector)
            if assessment is None or not assessment.is_clean:
                return _surface_unclean(
                    target_url,
                    request_id=request_id,
                    attempts=attempts,
                    strategy=strategy,
                    started_monotonic=started_monotonic,
                    assessment=assessment or UNREADABLE_SURFACE,
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

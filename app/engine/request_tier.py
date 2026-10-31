"""Anti-detect HTTP request execution tier."""

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from app.config import Settings
from app.engine.budget import (
    MIN_ESCALATE_REMAINING_SECONDS,
    elapsed_ms,
    remaining_total_seconds,
)
from app.engine.envelope import TIMEOUT_ERROR_BY_PHASE, build_error, build_success
from app.engine.work_lease import WorkLease
from app.infra.cookies import jar_cookies
from app.infra.detector import ChallengeDetector
from app.logging_config import get_logger, log_event
from app.schemas.enums import ErrorCategory, ExecutionMode, ExecutionTier, TimeoutPhase
from app.schemas.request import ScrapeRequest
from app.schemas.response import ScrapeError, ScrapeSuccess

logger = get_logger()

if TYPE_CHECKING:
    from botasaurus.request import HttpResponse


def run_request_tier(
    payload: ScrapeRequest,
    request_id: str,
    started_monotonic: float,
    lease: WorkLease,
    *,
    settings: Settings,
) -> ScrapeSuccess | ScrapeError | None:
    from botasaurus.request import Request

    target_url = str(payload.url)
    remaining_budget = remaining_total_seconds(settings, started_monotonic)
    if remaining_budget <= 0:
        lease.mark(
            TimeoutPhase.WORK,
            attempts=1,
            execution_tier=ExecutionTier.HTTP_REQUEST,
        )
        return build_error(
            target_url,
            TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.WORK],
            request_id=request_id,
            error_category=ErrorCategory.TIMEOUT,
            attempts=1,
            render_ms=elapsed_ms(started_monotonic),
            execution_tier=ExecutionTier.HTTP_REQUEST,
            timeout_phase=TimeoutPhase.WORK,
        )

    lease.mark(
        TimeoutPhase.WORK,
        attempts=1,
        execution_tier=ExecutionTier.HTTP_REQUEST,
    )

    req_headers = dict(payload.headers) if payload.headers else {}
    proxies = {"http": payload.proxy, "https": payload.proxy} if payload.proxy else None

    req = Request()
    resp: HttpResponse | None = None
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
            {str(k): str(v) for k, v in resp.headers.items()} if resp.headers else None
        )
        final_url = resp.url or target_url

        assessment = ChallengeDetector.detect(html, status_code, headers=headers_dict)
        render_ms = elapsed_ms(started_monotonic)

        is_clean_success = (
            assessment.is_clean
            and (200 <= status_code < 300)
            and len(html.strip()) > 0
            and not payload.wait_for_selector
            and not payload.scroll
        )

        if payload.execution_mode == ExecutionMode.AUTO and not is_clean_success:
            remaining = remaining_total_seconds(settings, started_monotonic)
            if remaining >= MIN_ESCALATE_REMAINING_SECONDS:
                log_event(
                    logger,
                    logging.INFO,
                    "request_tier_escalating",
                    request_id=request_id,
                    host=urlparse(target_url).hostname,
                    status=status_code,
                    blocked=assessment.blocked_detected,
                    challenge=assessment.challenge_detected,
                    remaining=remaining,
                )
                return None
            if not assessment.is_clean:
                log_event(
                    logger,
                    logging.INFO,
                    "request_tier_skip_escalate_challenge",
                    request_id=request_id,
                    host=urlparse(target_url).hostname,
                    remaining=remaining,
                    marker=assessment.detected_marker,
                )
                return build_error(
                    target_url,
                    "Challenge block detected",
                    request_id=request_id,
                    error_category=ErrorCategory.CHALLENGE_BLOCK,
                    attempts=1,
                    render_ms=render_ms,
                    execution_tier=ExecutionTier.HTTP_REQUEST,
                    assessment=assessment,
                )
            log_event(
                logger,
                logging.INFO,
                "request_tier_skip_escalate",
                request_id=request_id,
                host=urlparse(target_url).hostname,
                status=status_code,
                remaining=remaining,
            )

        if assessment.blocked_detected:
            return build_error(
                target_url,
                "Challenge block detected",
                request_id=request_id,
                error_category=ErrorCategory.CHALLENGE_BLOCK,
                attempts=1,
                render_ms=render_ms,
                execution_tier=ExecutionTier.HTTP_REQUEST,
                assessment=assessment,
            )

        return build_success(
            target_url,
            request_id=request_id,
            html=html,
            final_url=final_url,
            status_code=status_code,
            headers=headers_dict,
            attempts=1,
            render_ms=render_ms,
            execution_tier=ExecutionTier.HTTP_REQUEST,
            assessment=assessment,
            cookies=jar_cookies(resp.cookies, target_url, final_url),
        )
    finally:
        try:
            req.close()
        except Exception:
            pass

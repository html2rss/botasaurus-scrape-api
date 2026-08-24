"""Anti-detect HTTP request execution tier."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from botasaurus.request import Request

from app.config import Settings
from app.engine.envelope import build_error, build_success
from app.infra.detector import ChallengeDetector
from app.infra.scrape_progress import ScrapeProgress
from app.schemas import (
    ErrorCategory,
    ExecutionMode,
    ExecutionTier,
    ScrapeError,
    ScrapeRequest,
    ScrapeSuccess,
    TimeoutPhase,
)

logger = logging.getLogger("botasaurus_scrape_api")


def remaining_total_seconds(settings: Settings, started_monotonic: float) -> int:
    return max(
        1,
        int(settings.scrape_timeout_seconds - (time.monotonic() - started_monotonic)),
    )


def run_request_tier(
    engine: object,
    payload: ScrapeRequest,
    request_id: str,
    started_monotonic: float,
    progress: ScrapeProgress,
    *,
    settings: Settings,
) -> ScrapeSuccess | ScrapeError | None:
    del engine
    target_url = str(payload.url)
    remaining_budget = remaining_total_seconds(settings, started_monotonic)
    progress.mark(
        TimeoutPhase.WORK,
        attempts=1,
        execution_tier=ExecutionTier.HTTP_REQUEST,
    )

    req_headers = dict(payload.headers) if payload.headers else {}
    proxies = {"http": payload.proxy, "https": payload.proxy} if payload.proxy else None

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
            and not payload.scroll
        )

        if payload.execution_mode == ExecutionMode.AUTO and not is_clean_success:
            logger.info(
                "request_tier_escalating request_id=%s host=%s status=%d blocked=%s challenge=%s",
                request_id,
                urlparse(target_url).hostname,
                status_code,
                assessment.blocked_detected,
                assessment.challenge_detected,
            )
            return None

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
        )
    finally:
        try:
            req.close()
        except Exception:
            pass

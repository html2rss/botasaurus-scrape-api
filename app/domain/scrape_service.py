"""Scrape request orchestration between HTTP boundary and execution engine."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

from app.config import Settings
from app.engine import ScraperEngine
from app.engine.budget import elapsed_ms
from app.engine.work_lease import HostConcurrencyGate, LeaseSnapshot, WorkLease
from app.exceptions import RequestIdCollisionError
from app.infra.ops_telemetry import emit_terminal_telemetry
from app.infra.request_id import resolve_request_id
from app.logging_config import get_logger, log_event
from app.schemas.enums import ErrorCategory
from app.schemas.request import ScrapeRequest
from app.schemas.response import (
    ScrapeDiagnostics,
    ScrapeError,
    ScrapeSuccess,
    validation_error,
)
from app.security import UrlGuard, ValidationResult

logger = get_logger()


@dataclass(frozen=True, slots=True)
class ScrapeOutcome:
    body: ScrapeSuccess | ScrapeError
    status_code: int


def _emit_scrape_finished(
    *,
    payload: ScrapeRequest,
    host: str,
    body: ScrapeSuccess | ScrapeError,
    status_code: int,
    snapshot: LeaseSnapshot,
    started_monotonic: float,
) -> None:
    """One terminal log event per admitted scrape (replaces scrape_timeout/complete)."""
    diagnostics = body.diagnostics
    error_category = (
        body.error_category.value if isinstance(body, ScrapeError) else None
    )
    timeout_phase = (
        diagnostics.timeout_phase.value
        if diagnostics.timeout_phase is not None
        else None
    )
    tier = (
        diagnostics.execution_tier.value
        if diagnostics.execution_tier is not None
        else None
    )
    level = (
        logging.WARNING
        if isinstance(body, ScrapeError)
        and body.error_category == ErrorCategory.TIMEOUT
        else logging.INFO
    )
    log_event(
        logger,
        level,
        "scrape_finished",
        request_id=diagnostics.request_id,
        host=host,
        execution_mode=payload.execution_mode.value,
        navigation_mode=payload.navigation_mode.value,
        tier=tier,
        status=status_code,
        error_category=error_category,
        timeout_phase=timeout_phase,
        attempts=diagnostics.attempts,
        warm_hit=snapshot.warm_hit,
        render_ms=diagnostics.render_ms,
        duration_ms=elapsed_ms(started_monotonic),
    )


def _finish_outcome(
    *,
    payload: ScrapeRequest,
    host: str,
    body: ScrapeSuccess | ScrapeError,
    status_code: int,
    lease: WorkLease,
    started_monotonic: float,
) -> ScrapeOutcome:
    """Shared terminal path: lease snapshot, ops telemetry, scrape_finished, outcome."""
    snapshot = lease.snapshot()
    if isinstance(body, ScrapeError):
        emit_terminal_telemetry(
            body,
            http_status=status_code,
            warm_hit=snapshot.warm_hit,
        )
    _emit_scrape_finished(
        payload=payload,
        host=host,
        body=body,
        status_code=status_code,
        snapshot=snapshot,
        started_monotonic=started_monotonic,
    )
    return ScrapeOutcome(body=body, status_code=status_code)


class ScrapeService:
    """Owns request-id resolution, URL guardrails, WorkLease execution, status mapping."""

    def __init__(
        self,
        *,
        settings: Settings,
        engine: ScraperEngine,
        executor: ThreadPoolExecutor,
        host_gate: HostConcurrencyGate | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.executor = executor
        self.host_gate = host_gate or HostConcurrencyGate(settings.scrape_max_per_host)

    async def process(
        self,
        payload: ScrapeRequest,
        *,
        inbound_request_id: str | None = None,
    ) -> ScrapeOutcome:
        """Resolve the request id, enforce SSRF guardrails, then execute."""
        target_url = str(payload.url)
        host = payload.url.host
        request_id = resolve_request_id(inbound_request_id, host=host)
        blocked = self._guard_outcome(payload, target_url, request_id=request_id)
        if blocked is not None:
            return blocked
        return await self._run(payload, request_id=request_id)

    def _guard_outcome(
        self,
        payload: ScrapeRequest,
        target_url: str,
        *,
        request_id: str,
    ) -> ScrapeOutcome | None:
        target_validation = UrlGuard.validate(target_url)
        if not target_validation.is_allowed:
            return self._validation_outcome(
                target_url,
                target_validation,
                request_id=request_id,
                default_message="Target URL is blocked",
            )
        if payload.proxy:
            proxy_validation = UrlGuard.validate_proxy(str(payload.proxy))
            if not proxy_validation.is_allowed:
                return self._validation_outcome(
                    target_url,
                    proxy_validation,
                    request_id=request_id,
                    default_message="Proxy URL is invalid or blocked",
                )
        return None

    @staticmethod
    def _validation_outcome(
        url: str,
        validation: ValidationResult,
        *,
        request_id: str,
        default_message: str,
    ) -> ScrapeOutcome:
        return ScrapeOutcome(
            body=validation_error(
                url,
                validation.error_message or default_message,
                request_id=request_id,
            ),
            status_code=validation.status_code,
        )

    async def _run(
        self,
        payload: ScrapeRequest,
        *,
        request_id: str,
    ) -> ScrapeOutcome:
        target_url = str(payload.url)
        host = payload.url.host or ""
        lease = WorkLease(
            settings=self.settings,
            executor=self.executor,
            host_gate=self.host_gate,
        )
        started_monotonic = time.monotonic()
        deadline_monotonic = started_monotonic + self.settings.scrape_timeout_seconds

        try:
            result = await lease.run(
                host=host,
                work=partial(
                    self.engine.execute,
                    payload,
                    deadline_monotonic,
                    request_id=request_id,
                    lease=lease,
                ),
            )
        except RequestIdCollisionError:
            return _finish_outcome(
                payload=payload,
                host=host,
                body=ScrapeError(
                    url=target_url,
                    error="Request id collision detected",
                    error_category=ErrorCategory.NAVIGATION_ERROR,
                    diagnostics=ScrapeDiagnostics(
                        request_id=request_id,
                        attempts=0,
                        render_ms=0,
                    ),
                ),
                status_code=502,
                lease=lease,
                started_monotonic=started_monotonic,
            )
        except TimeoutError:
            return _finish_outcome(
                payload=payload,
                host=host,
                body=lease.timeout_error(
                    target_url,
                    request_id=request_id,
                    started_monotonic=started_monotonic,
                ),
                status_code=504,
                lease=lease,
                started_monotonic=started_monotonic,
            )

        return _finish_outcome(
            payload=payload,
            host=host,
            body=result,
            status_code=200 if isinstance(result, ScrapeSuccess) else 502,
            lease=lease,
            started_monotonic=started_monotonic,
        )

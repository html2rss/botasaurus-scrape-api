"""Scrape request orchestration between HTTP boundary and execution engine."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

from app.config import Settings
from app.engine import ScraperEngine
from app.engine.work_lease import HostConcurrencyGate, WorkLease
from app.exceptions import RequestIdCollisionError
from app.infra.ops_telemetry import emit_terminal_telemetry
from app.infra.request_id import resolve_request_id
from app.logging_config import get_logger
from app.schemas.enums import ErrorCategory, TimeoutPhase
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
            collision_result = ScrapeError(
                url=target_url,
                error="Request id collision detected",
                error_category=ErrorCategory.NAVIGATION_ERROR,
                diagnostics=ScrapeDiagnostics(
                    request_id=request_id,
                    attempts=0,
                    render_ms=0,
                ),
            )
            emit_terminal_telemetry(collision_result, http_status=502)
            return ScrapeOutcome(body=collision_result, status_code=502)
        except TimeoutError:
            timeout_result = lease.timeout_error(
                target_url,
                request_id=request_id,
                started_monotonic=started_monotonic,
            )
            phase = timeout_result.diagnostics.timeout_phase or TimeoutPhase.QUEUE
            logger.warning(
                "scrape_timeout host=%s mode=%s timeout_seconds=%d phase=%s attempts=%d",
                host,
                payload.navigation_mode.value,
                self.settings.scrape_timeout_seconds,
                phase.value,
                timeout_result.diagnostics.attempts,
            )
            emit_terminal_telemetry(
                timeout_result,
                http_status=504,
                warm_hit=lease.snapshot().warm_hit,
            )
            return ScrapeOutcome(body=timeout_result, status_code=504)

        status_code = 200 if isinstance(result, ScrapeSuccess) else 502
        if isinstance(result, ScrapeError):
            emit_terminal_telemetry(
                result,
                http_status=status_code,
                warm_hit=lease.snapshot().warm_hit,
            )
        logger.info(
            "scrape_complete request_id=%s host=%s mode=%s tier=%s attempts=%s status=%d error_category=%s",
            result.diagnostics.request_id,
            host,
            payload.navigation_mode.value,
            result.diagnostics.execution_tier.value
            if result.diagnostics.execution_tier
            else None,
            result.diagnostics.attempts,
            status_code,
            result.error_category.value if isinstance(result, ScrapeError) else None,
        )
        return ScrapeOutcome(body=result, status_code=status_code)

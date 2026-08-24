"""Scrape request orchestration between HTTP boundary and execution engine."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from urllib.parse import urlparse

from app.config import Settings
from app.engine import ScraperEngine
from app.exceptions import RequestIdCollisionError
from app.infra.ops_telemetry import emit_terminal_telemetry
from app.infra.scrape_progress import ScrapeProgress
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
    """Owns URL validation, threadpool execution, status mapping, and telemetry."""

    def __init__(
        self,
        *,
        settings: Settings,
        engine: ScraperEngine,
        executor: ThreadPoolExecutor,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.executor = executor

    def validate_target(self, url: str) -> ValidationResult:
        return UrlGuard.validate(url)

    def validate_proxy(self, proxy_url: str) -> ValidationResult:
        return UrlGuard.validate_proxy(proxy_url)

    def validation_outcome(
        self,
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

    @staticmethod
    def build_timeout_error(
        url: str,
        *,
        request_id: str,
        started_monotonic: float,
        progress: ScrapeProgress,
        timeout_seconds: int,
    ) -> ScrapeError:
        snap = progress.snapshot()
        phase = snap.phase
        render_ms = int((time.monotonic() - started_monotonic) * 1000)
        return ScrapeError(
            url=url,
            error=(
                f"Scrape timed out after {timeout_seconds} seconds (phase={phase.value})"
            ),
            error_category=ErrorCategory.TIMEOUT,
            diagnostics=ScrapeDiagnostics(
                request_id=request_id,
                attempts=snap.attempts,
                strategy_used=snap.strategy_used,
                render_ms=render_ms,
                execution_tier=snap.execution_tier,
                timeout_phase=phase,
            ),
        )

    async def run(
        self,
        payload: ScrapeRequest,
        *,
        request_id: str,
    ) -> ScrapeOutcome:
        target_url = str(payload.url)
        started_monotonic = time.monotonic()
        deadline_monotonic = started_monotonic + self.settings.scrape_timeout_seconds
        progress = ScrapeProgress()

        try:
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    self.executor,
                    partial(
                        self.engine.execute,
                        payload,
                        deadline_monotonic,
                        request_id=request_id,
                        progress=progress,
                    ),
                ),
                timeout=self.settings.scrape_timeout_seconds,
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
            timeout_result = self.build_timeout_error(
                target_url,
                request_id=request_id,
                started_monotonic=started_monotonic,
                progress=progress,
                timeout_seconds=self.settings.scrape_timeout_seconds,
            )
            phase = timeout_result.diagnostics.timeout_phase or TimeoutPhase.QUEUE
            logger.warning(
                "scrape_timeout host=%s mode=%s timeout_seconds=%d phase=%s attempts=%d",
                urlparse(target_url).hostname,
                payload.navigation_mode,
                self.settings.scrape_timeout_seconds,
                phase.value,
                timeout_result.diagnostics.attempts,
            )
            emit_terminal_telemetry(timeout_result, http_status=504)
            return ScrapeOutcome(body=timeout_result, status_code=504)

        status_code = 200 if isinstance(result, ScrapeSuccess) else 502
        if isinstance(result, ScrapeError):
            emit_terminal_telemetry(result, http_status=status_code)
        logger.info(
            "scrape_complete request_id=%s host=%s mode=%s tier=%s attempts=%s status=%d error_category=%s",
            result.diagnostics.request_id,
            urlparse(target_url).hostname,
            payload.navigation_mode,
            result.diagnostics.execution_tier,
            result.diagnostics.attempts,
            status_code,
            result.error_category if isinstance(result, ScrapeError) else None,
        )
        return ScrapeOutcome(body=result, status_code=status_code)

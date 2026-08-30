"""Scrape execution orchestrator across HTTP and browser tiers."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from app.config import Settings
from app.engine.browser_tier import run_browser_tier
from app.engine.budget import elapsed_ms, is_timeout_exception, remaining_total_seconds
from app.engine.envelope import TIMEOUT_ERROR_BY_PHASE, build_error
from app.engine.request_tier import run_request_tier
from app.engine.session import ScrapeSession
from app.engine.warm_pool import DriverFingerprint, WarmDriverPool
from app.engine.work_lease import WorkLease
from app.exceptions import RequestIdCollisionError
from app.infra.runtime_cleanup import (
    prune_orphan_runtime_dirs,
    runtime_root_low_on_space,
)
from app.logging_config import get_logger
from app.schemas.enums import (
    ErrorCategory,
    ExecutionMode,
    ExecutionTier,
    NavigationMode,
    TimeoutPhase,
)
from app.schemas.request import ScrapeRequest
from app.schemas.response import ScrapeError, ScrapeSuccess

logger = get_logger()


class ScraperEngine:
    """Deep module orchestrating anti-detect HTTP and browser execution tiers."""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime_root: Path | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_root = runtime_root or self.settings.runtime_root
        self._active_request_ids: set[str] = set()
        self._active_request_ids_lock = threading.Lock()
        self.warm_pool: WarmDriverPool | None = None

    def register_request_id(self, request_id: str) -> None:
        with self._active_request_ids_lock:
            if request_id in self._active_request_ids:
                raise RequestIdCollisionError(request_id)
            self._active_request_ids.add(request_id)

    def unregister_request_id(self, request_id: str) -> None:
        with self._active_request_ids_lock:
            self._active_request_ids.discard(request_id)

    def prune_runtime_dirs(self) -> int:
        # Active request ids and spare dirs are snapshotted atomically for prune.
        with self._active_request_ids_lock:
            protected: set[Path] = (
                self.warm_pool.live_spare_dirs()
                if self.warm_pool is not None
                else set()
            )
            return prune_orphan_runtime_dirs(
                self.runtime_root,
                set(self._active_request_ids),
                protected_dirs=protected,
            )

    def prepare_runtime_for_request(self) -> None:
        if runtime_root_low_on_space(
            self.runtime_root,
            min_free_bytes=self.settings.scrape_runtime_min_free_bytes,
        ):
            logger.info("runtime_root_low_on_space path=%s", self.runtime_root)
        self.prune_runtime_dirs()
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def execute(
        self,
        payload: ScrapeRequest,
        deadline_monotonic: float | None = None,
        *,
        request_id: str | None = None,
        lease: WorkLease | None = None,
    ) -> ScrapeSuccess | ScrapeError:
        target_url = str(payload.url)
        resolved_request_id = request_id or str(uuid.uuid4())
        now = time.monotonic()
        # When the API supplies a deadline (computed at submission), budget math
        # must use that submission start — not the post-queue worker clock — or
        # a long queue wait grants a second full timeout.
        if deadline_monotonic is not None:
            started_monotonic = (
                deadline_monotonic - self.settings.scrape_timeout_seconds
            )
        else:
            started_monotonic = now
        lease = lease or WorkLease.tracking_only(self.settings)

        if lease.aborted:
            # Outer deadline reclaimed before/without session enter — exit without
            # Chromium so host-gate release stays honest vs live browsers.
            phase = lease.snapshot().phase
            return build_error(
                target_url,
                TIMEOUT_ERROR_BY_PHASE[phase],
                request_id=resolved_request_id,
                error_category=ErrorCategory.TIMEOUT,
                timeout_phase=phase,
            )

        if deadline_monotonic is not None and now >= deadline_monotonic:
            lease.mark(TimeoutPhase.QUEUE)
            return build_error(
                target_url,
                TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.QUEUE],
                request_id=resolved_request_id,
                error_category=ErrorCategory.TIMEOUT,
                timeout_phase=TimeoutPhase.QUEUE,
            )

        warm_fp: DriverFingerprint | None = None
        try:
            with ScrapeSession(self, resolved_request_id, lease=lease) as session:
                should_try_request_tier = (
                    payload.execution_mode == ExecutionMode.REQUEST
                    or (
                        payload.execution_mode == ExecutionMode.AUTO
                        and payload.navigation_mode == NavigationMode.AUTO
                        and not payload.wait_for_selector
                        and not payload.scroll
                    )
                )

                if should_try_request_tier:
                    try:
                        request_result = run_request_tier(
                            payload,
                            resolved_request_id,
                            started_monotonic,
                            lease,
                            settings=self.settings,
                        )
                        if request_result is not None:
                            warm_fp = session.warm_fingerprint
                            return request_result
                    except Exception as exc:
                        logger.info(
                            "request_tier_failed request_id=%s host=%s error=%s",
                            resolved_request_id,
                            urlparse(target_url).hostname,
                            str(exc),
                        )
                        if payload.execution_mode == ExecutionMode.REQUEST:
                            render_ms = elapsed_ms(started_monotonic)
                            is_timeout = is_timeout_exception(exc)
                            warm_fp = session.warm_fingerprint
                            return build_error(
                                target_url,
                                (
                                    TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.WORK]
                                    if is_timeout
                                    else str(exc)
                                ),
                                request_id=resolved_request_id,
                                attempts=1,
                                render_ms=render_ms,
                                error_category=(
                                    ErrorCategory.TIMEOUT
                                    if is_timeout
                                    else ErrorCategory.NAVIGATION_ERROR
                                ),
                                execution_tier=ExecutionTier.HTTP_REQUEST,
                                timeout_phase=TimeoutPhase.WORK if is_timeout else None,
                            )

                if remaining_total_seconds(self.settings, started_monotonic) <= 0:
                    lease.mark(
                        TimeoutPhase.BOOT,
                        execution_tier=ExecutionTier.BROWSER_DRIVER,
                    )
                    warm_fp = session.warm_fingerprint
                    return build_error(
                        target_url,
                        TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.BOOT],
                        request_id=resolved_request_id,
                        attempts=0,
                        render_ms=elapsed_ms(started_monotonic),
                        error_category=ErrorCategory.TIMEOUT,
                        execution_tier=ExecutionTier.BROWSER_DRIVER,
                        timeout_phase=TimeoutPhase.BOOT,
                    )

                result = run_browser_tier(
                    payload,
                    session,
                    started_monotonic,
                    lease,
                    settings=self.settings,
                )
                warm_fp = session.warm_fingerprint
                return result
        finally:
            if self.warm_pool is not None and warm_fp is not None:
                self.warm_pool.notify_scrape_finished(warm_fp)

"""WorkLease: admit, deadline, phase snapshot, and Chromium reclaim ownership."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TypeVar

from app.config import Settings
from app.engine.budget import elapsed_ms
from app.engine.envelope import TIMEOUT_ERROR_BY_PHASE
from app.logging_config import get_logger
from app.schemas.enums import (
    ErrorCategory,
    ExecutionTier,
    NavigationMode,
    TimeoutPhase,
)
from app.schemas.response import ScrapeDiagnostics, ScrapeError

logger = get_logger()

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LeaseSnapshot:
    phase: TimeoutPhase = TimeoutPhase.QUEUE
    attempts: int = 0
    strategy_used: NavigationMode | None = None
    execution_tier: ExecutionTier | None = None
    warm_hit: bool | None = None


class HostConcurrencyGate:
    """Process-wide per-host admit gate (one fact: max concurrent scrapes per host)."""

    def __init__(self, max_per_host: int) -> None:
        if max_per_host < 1:
            raise ValueError("max_per_host must be >= 1")
        self._max_per_host = max_per_host
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}

    def _semaphore(self, host: str) -> threading.BoundedSemaphore:
        with self._lock:
            sem = self._semaphores.get(host)
            if sem is None:
                sem = threading.BoundedSemaphore(self._max_per_host)
                self._semaphores[host] = sem
            return sem

    def acquire(self, host: str, timeout: float) -> bool:
        return self._semaphore(host).acquire(timeout=max(0.0, timeout))

    def release(self, host: str) -> None:
        self._semaphore(host).release()


class WorkLease:
    """Owns admit → run → terminal timeout; reclaim kills the session driver once.

    Progress phase lives on the lease (replaces ScrapeProgress). Outer deadline
    calls register_reclaim hooks (session force-close); Future.cancel is not the
    Chromium reclaim story.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        executor: ThreadPoolExecutor | None = None,
        host_gate: HostConcurrencyGate | None = None,
    ) -> None:
        self.settings = settings
        self.executor = executor
        self.host_gate = host_gate
        self._lock = threading.Lock()
        self._snap = LeaseSnapshot()
        self._reclaim_hooks: list[Callable[[], None]] = []
        self._inflight: asyncio.Future[object] | None = None

    @classmethod
    def tracking_only(cls, settings: Settings | None = None) -> WorkLease:
        """Mark/snapshot-only lease for direct engine.execute tests."""
        from app.config import get_settings

        return cls(settings=settings or get_settings())

    def mark(
        self,
        phase: TimeoutPhase,
        *,
        attempts: int | None = None,
        strategy_used: NavigationMode | None = None,
        execution_tier: ExecutionTier | None = None,
    ) -> None:
        fields = {
            k: v
            for k, v in (
                ("attempts", attempts),
                ("strategy_used", strategy_used),
                ("execution_tier", execution_tier),
            )
            if v is not None
        }
        with self._lock:
            self._snap = replace(self._snap, phase=phase, **fields)

    def set_warm_hit(self, warm_hit: bool) -> None:
        with self._lock:
            self._snap = replace(self._snap, warm_hit=warm_hit)

    def snapshot(self) -> LeaseSnapshot:
        with self._lock:
            return self._snap

    def register_reclaim(self, hook: Callable[[], None]) -> None:
        with self._lock:
            self._reclaim_hooks.append(hook)

    def reclaim(self) -> None:
        with self._lock:
            hooks = list(self._reclaim_hooks)
        for hook in hooks:
            try:
                hook()
            except Exception as exc:
                logger.debug("lease_reclaim_hook_failed error=%s", str(exc))

    def timeout_error(
        self,
        url: str,
        *,
        request_id: str,
        started_monotonic: float,
    ) -> ScrapeError:
        """Single timeout envelope builder fed by lease phase."""
        snap = self.snapshot()
        phase = snap.phase
        return ScrapeError(
            url=url,
            error=TIMEOUT_ERROR_BY_PHASE[phase],
            error_category=ErrorCategory.TIMEOUT,
            diagnostics=ScrapeDiagnostics(
                request_id=request_id,
                attempts=snap.attempts,
                strategy_used=snap.strategy_used,
                render_ms=elapsed_ms(started_monotonic),
                execution_tier=snap.execution_tier,
                timeout_phase=phase,
            ),
        )

    async def run(self, *, host: str, work: Callable[[], T]) -> T:
        """Admit on host gate, run work on executor, reclaim on deadline."""
        if self.executor is None or self.host_gate is None:
            raise RuntimeError("WorkLease.run requires executor and host_gate")

        timeout_seconds = self.settings.scrape_timeout_seconds
        started_monotonic = time.monotonic()
        deadline = started_monotonic + timeout_seconds

        remaining_admit = deadline - time.monotonic()
        admitted = await asyncio.to_thread(
            self.host_gate.acquire, host, remaining_admit
        )
        if not admitted:
            self.mark(TimeoutPhase.QUEUE)
            raise TimeoutError("host concurrency admit deadline")

        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.mark(TimeoutPhase.QUEUE)
                raise TimeoutError("scrape deadline before executor submit")

            loop = asyncio.get_running_loop()
            awaitable = asyncio.ensure_future(loop.run_in_executor(self.executor, work))
            # Strong ref: shielded tasks are only weakly held by the loop.
            self._inflight = awaitable
            try:
                return await asyncio.wait_for(
                    asyncio.shield(awaitable), timeout=remaining
                )
            except TimeoutError:
                self.reclaim()
                raise
            finally:
                if awaitable.done():
                    self._inflight = None
                else:
                    awaitable.add_done_callback(lambda _f: None)
        finally:
            self.host_gate.release(host)

# app/scrape_progress.py
from __future__ import annotations

import threading
from dataclasses import dataclass

from app.schemas import ExecutionTier, NavigationMode, TimeoutPhase


@dataclass(frozen=True, slots=True)
class ScrapeProgressSnapshot:
    phase: TimeoutPhase
    attempts: int
    strategy_used: NavigationMode | None
    execution_tier: ExecutionTier | None


class ScrapeProgress:
    """Thread-safe scrape stage tracker for handler-timeout diagnostics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase = TimeoutPhase.QUEUE
        self._attempts = 0
        self._strategy_used: NavigationMode | None = None
        self._execution_tier: ExecutionTier | None = None

    def mark(
        self,
        phase: TimeoutPhase,
        *,
        attempts: int | None = None,
        strategy_used: NavigationMode | None = None,
        execution_tier: ExecutionTier | None = None,
    ) -> None:
        with self._lock:
            self._phase = phase
            if attempts is not None:
                self._attempts = attempts
            if strategy_used is not None:
                self._strategy_used = strategy_used
            if execution_tier is not None:
                self._execution_tier = execution_tier

    def snapshot(self) -> ScrapeProgressSnapshot:
        with self._lock:
            return ScrapeProgressSnapshot(
                phase=self._phase,
                attempts=self._attempts,
                strategy_used=self._strategy_used,
                execution_tier=self._execution_tier,
            )

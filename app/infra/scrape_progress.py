"""In-memory scrape progress tracking for timeout phase diagnostics."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace

from app.schemas.enums import ExecutionTier, NavigationMode, TimeoutPhase


@dataclass(frozen=True, slots=True)
class ScrapeProgressSnapshot:
    phase: TimeoutPhase = TimeoutPhase.QUEUE
    attempts: int = 0
    strategy_used: NavigationMode | None = None
    execution_tier: ExecutionTier | None = None
    warm_hit: bool | None = None


class ScrapeProgress:
    """Thread-safe scrape stage tracker for handler-timeout diagnostics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap = ScrapeProgressSnapshot()

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

    def snapshot(self) -> ScrapeProgressSnapshot:
        with self._lock:
            return self._snap

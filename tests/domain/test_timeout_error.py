"""WorkLease timeout envelope builder (single owner)."""

from __future__ import annotations

import time
import unittest

from app.engine.envelope import TIMEOUT_ERROR_BY_PHASE
from app.engine.work_lease import WorkLease
from app.schemas.enums import ExecutionTier, NavigationMode, TimeoutPhase

_URL = "https://example.com"


class WorkLeaseTimeoutErrorTests(unittest.TestCase):
    def test_queue_phase_keeps_zero_attempts(self) -> None:
        result = WorkLease.tracking_only().timeout_error(
            _URL,
            request_id="req-queue",
            started_monotonic=time.monotonic(),
        )
        self.assertEqual(result.error_category.value, "timeout")
        self.assertEqual(result.error, TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.QUEUE])
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.QUEUE)
        self.assertEqual(result.diagnostics.attempts, 0)
        self.assertIsNone(result.diagnostics.strategy_used)

    def test_boot_phase_message(self) -> None:
        lease = WorkLease.tracking_only()
        lease.mark(TimeoutPhase.BOOT, execution_tier=ExecutionTier.BROWSER_DRIVER)
        result = lease.timeout_error(
            _URL,
            request_id="req-boot",
            started_monotonic=time.monotonic(),
        )
        self.assertEqual(result.error, TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.BOOT])
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.BOOT)

    def test_work_phase_preserves_attempts_and_strategy(self) -> None:
        lease = WorkLease.tracking_only()
        lease.mark(
            TimeoutPhase.WORK,
            attempts=2,
            strategy_used=NavigationMode.GOOGLE_GET,
            execution_tier=ExecutionTier.BROWSER_DRIVER,
        )
        result = lease.timeout_error(
            _URL,
            request_id="req-work",
            started_monotonic=time.monotonic() - 1,
        )
        diagnostics = result.diagnostics
        self.assertEqual(diagnostics.timeout_phase, TimeoutPhase.WORK)
        self.assertEqual(diagnostics.attempts, 2)
        self.assertEqual(diagnostics.strategy_used, NavigationMode.GOOGLE_GET)
        self.assertEqual(diagnostics.execution_tier, ExecutionTier.BROWSER_DRIVER)
        self.assertEqual(result.error, TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.WORK])
        self.assertGreaterEqual(diagnostics.render_ms, 0)


if __name__ == "__main__":
    unittest.main()

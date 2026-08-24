"""ScrapeService.build_timeout_error phase and diagnostics mapping."""

from __future__ import annotations

import time
import unittest

from app.domain.scrape_service import ScrapeService
from app.infra.scrape_progress import ScrapeProgress
from app.schemas.enums import ExecutionTier, NavigationMode, TimeoutPhase

_URL = "https://example.com"


class BuildTimeoutErrorTests(unittest.TestCase):
    def test_queue_phase_keeps_zero_attempts(self):
        result = ScrapeService.build_timeout_error(
            _URL,
            request_id="req-queue",
            started_monotonic=time.monotonic(),
            progress=ScrapeProgress(),
            timeout_seconds=45,
        )
        self.assertEqual(result.error_category.value, "timeout")
        self.assertIn("phase=queue", result.error)
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.QUEUE)
        self.assertEqual(result.diagnostics.attempts, 0)
        self.assertIsNone(result.diagnostics.strategy_used)

    def test_work_phase_preserves_attempts_and_strategy(self):
        progress = ScrapeProgress()
        progress.mark(
            TimeoutPhase.WORK,
            attempts=2,
            strategy_used=NavigationMode.GOOGLE_GET,
            execution_tier=ExecutionTier.BROWSER_DRIVER,
        )
        result = ScrapeService.build_timeout_error(
            _URL,
            request_id="req-work",
            started_monotonic=time.monotonic() - 1,
            progress=progress,
            timeout_seconds=45,
        )
        diagnostics = result.diagnostics
        self.assertEqual(diagnostics.timeout_phase, TimeoutPhase.WORK)
        self.assertEqual(diagnostics.attempts, 2)
        self.assertEqual(diagnostics.strategy_used, NavigationMode.GOOGLE_GET)
        self.assertEqual(diagnostics.execution_tier, ExecutionTier.BROWSER_DRIVER)
        self.assertIn("phase=work", result.error)
        self.assertGreaterEqual(diagnostics.render_ms, 0)


if __name__ == "__main__":
    unittest.main()

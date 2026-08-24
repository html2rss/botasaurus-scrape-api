"""ScrapeProgress snapshot unit tests."""

from __future__ import annotations

import unittest

from app.infra.scrape_progress import ScrapeProgress, ScrapeProgressSnapshot
from app.schemas.enums import ExecutionTier, NavigationMode, TimeoutPhase


class ScrapeProgressTests(unittest.TestCase):
    def test_snapshot_defaults_to_queue(self):
        snap: ScrapeProgressSnapshot = ScrapeProgress().snapshot()
        self.assertEqual(snap.phase, TimeoutPhase.QUEUE)
        self.assertEqual(snap.attempts, 0)
        self.assertIsNone(snap.strategy_used)
        self.assertIsNone(snap.execution_tier)

    def test_mark_updates_snapshot(self):
        progress = ScrapeProgress()
        progress.mark(
            TimeoutPhase.WORK,
            attempts=2,
            strategy_used=NavigationMode.GET,
            execution_tier=ExecutionTier.BROWSER_DRIVER,
        )
        snap = progress.snapshot()
        self.assertEqual(snap.phase, TimeoutPhase.WORK)
        self.assertEqual(snap.attempts, 2)
        self.assertEqual(snap.strategy_used, NavigationMode.GET)
        self.assertEqual(snap.execution_tier, ExecutionTier.BROWSER_DRIVER)


if __name__ == "__main__":
    unittest.main()

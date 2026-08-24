"""Pure unit tests for wall-clock budget math clamps and composition."""

from __future__ import annotations

import time
import unittest

from app.config import get_settings
from app.engine.budget import (
    browser_step_budget_seconds,
    elapsed_ms,
    is_timeout_exception,
    remaining_total_seconds,
    remaining_work_seconds,
)


class BudgetMathTests(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.now = time.monotonic()

    def test_elapsed_ms_is_non_negative_and_scales(self):
        self.assertGreaterEqual(elapsed_ms(self.now), 0)
        self.assertGreaterEqual(elapsed_ms(self.now - 1.5), 1500)

    def test_remaining_total_counts_down_from_scrape_timeout(self):
        fresh = remaining_total_seconds(self.settings, self.now)
        self.assertLessEqual(fresh, self.settings.scrape_timeout_seconds)
        self.assertGreaterEqual(fresh, self.settings.scrape_timeout_seconds - 1)

    def test_remaining_total_floors_at_one_when_exhausted(self):
        exhausted = self.now - (self.settings.scrape_timeout_seconds + 60)
        self.assertEqual(remaining_total_seconds(self.settings, exhausted), 1)

    def test_remaining_work_floors_at_one_when_exhausted(self):
        exhausted = self.now - (self.settings.scrape_work_timeout_seconds + 60)
        self.assertEqual(remaining_work_seconds(self.settings, exhausted), 1)

    def test_browser_step_budget_takes_the_tighter_constraint(self):
        # Total budget nearly burnt, work budget fresh: total wins.
        almost_burnt = self.now - (self.settings.scrape_timeout_seconds - 5)
        tight_total = browser_step_budget_seconds(self.settings, almost_burnt, self.now)
        self.assertLessEqual(tight_total, 5)

        # Both fresh: bounded by the smaller work budget.
        fresh = browser_step_budget_seconds(self.settings, self.now, self.now)
        self.assertLessEqual(fresh, self.settings.scrape_work_timeout_seconds)
        self.assertGreaterEqual(fresh, 1)

    def test_browser_step_budget_never_returns_zero_or_negative(self):
        long_ago = self.now - 10_000
        self.assertEqual(
            browser_step_budget_seconds(self.settings, long_ago, long_ago), 1
        )


class TimeoutExceptionClassificationTests(unittest.TestCase):
    def test_matches_timeout_messages_case_insensitively(self):
        self.assertTrue(is_timeout_exception(TimeoutError("navigation Timeout")))
        self.assertTrue(is_timeout_exception(RuntimeError("HTTP read TIMEOUT")))

    def test_ignores_non_timeout_messages(self):
        self.assertFalse(is_timeout_exception(RuntimeError("connection refused")))


if __name__ == "__main__":
    unittest.main()

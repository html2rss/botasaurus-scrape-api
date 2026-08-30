"""wait_for_readiness fail-closed on unreadable mid-wait surfaces."""

from __future__ import annotations

import unittest
from typing import Any

from app.engine.strategies import wait_for_readiness
from app.infra.detector import UNREADABLE_SURFACE


class _UnreadableWaitDriver:
    wait_calls = 0

    def wait_for_element(self, *_args: object, **_kwargs: Any) -> None:
        type(self).wait_calls += 1
        raise TimeoutError("element not found")

    @property
    def page_html(self) -> str:
        raise RuntimeError("tab unreadable")


class WaitForReadinessTests(unittest.TestCase):
    def test_unreadable_mid_wait_fails_closed_without_budget_burn(self) -> None:
        _UnreadableWaitDriver.wait_calls = 0
        assessment = wait_for_readiness(
            _UnreadableWaitDriver(),  # type: ignore[arg-type]
            selector="#content",
            timeout_seconds=6,
        )
        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertEqual(assessment.detected_marker, UNREADABLE_SURFACE.detected_marker)
        # One chunk then fail closed — not three 2s burns.
        self.assertEqual(_UnreadableWaitDriver.wait_calls, 1)


if __name__ == "__main__":
    unittest.main()

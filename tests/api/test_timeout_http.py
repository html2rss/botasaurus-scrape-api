"""HTTP 504 envelope carries progress-derived timeout diagnostics."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.infra.scrape_progress import ScrapeProgress
from app.schemas.enums import ErrorCategory, ExecutionTier, TimeoutPhase
from app.schemas.request import ScrapeRequest
from app.schemas.response import ScrapeDiagnostics, ScrapeError
from tests.support.http import ExecuteSideEffect, test_client

_URL = "https://example.com"


class HandlerTimeoutHttpTests(unittest.TestCase):
    def test_scrape_handler_timeout_uses_progress(self):
        def fake_execute(
            payload: ScrapeRequest,
            deadline_monotonic: float | None = None,
            *,
            request_id: str | None = None,
            progress: ScrapeProgress | None = None,
        ) -> ScrapeError:
            del payload, deadline_monotonic, request_id
            assert progress is not None
            progress.mark(
                TimeoutPhase.BOOT, execution_tier=ExecutionTier.BROWSER_DRIVER
            )
            # Sentinel only: the patched wait_for raises TimeoutError, so the
            # 504 envelope must come from the handler's own timeout path.
            return ScrapeError(
                url=_URL,
                error="sentinel-discarded",
                error_category=ErrorCategory.NAVIGATION_ERROR,
                diagnostics=ScrapeDiagnostics(request_id="sentinel"),
            )

        side_effect: ExecuteSideEffect = fake_execute

        async def boom(awaitable: object, timeout: float | None = None) -> None:
            del timeout
            await awaitable  # type: ignore[misc]
            raise TimeoutError

        with (
            test_client(execute_side_effect=side_effect) as client,
            patch("asyncio.wait_for", side_effect=boom),
        ):
            response = client.post(
                "/scrape",
                json={
                    "url": _URL,
                    "execution_mode": "browser",
                    "navigation_mode": "get",
                },
            )

        self.assertEqual(response.status_code, 504)
        body = response.json()
        self.assertEqual(body["error_category"], "timeout")
        self.assertEqual(body["diagnostics"]["timeout_phase"], "boot")
        self.assertEqual(body["diagnostics"]["attempts"], 0)
        self.assertEqual(body["diagnostics"]["execution_tier"], "browser_driver")
        self.assertEqual(body["error"], "Browser failed to start in time")


if __name__ == "__main__":
    unittest.main()

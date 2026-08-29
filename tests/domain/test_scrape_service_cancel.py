"""ScrapeService cancels queued executor work on outer timeout."""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from app.config import get_settings
from app.domain.scrape_service import ScrapeOutcome, ScrapeService
from app.engine import ScraperEngine
from app.schemas.enums import ErrorCategory, TimeoutPhase
from app.schemas.response import ScrapeError
from tests.support.factories import scrape_request


class ScrapeServiceCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_cancels_executor_future(self) -> None:
        settings = get_settings()
        engine = ScraperEngine(settings=settings)
        service = ScrapeService(
            settings=settings,
            engine=engine,
            executor=ThreadPoolExecutor(max_workers=1),
        )
        future = MagicMock()
        future.cancel = MagicMock(return_value=True)

        async def boom(awaitable: object, timeout: float | None = None) -> None:
            del awaitable, timeout
            raise TimeoutError

        with (
            patch("asyncio.get_running_loop") as mock_loop,
            patch("asyncio.wait_for", side_effect=boom),
        ):
            mock_loop.return_value.run_in_executor = MagicMock(return_value=future)
            outcome = await service.process(scrape_request())

        future.cancel.assert_called_once()
        self.assertIsInstance(outcome, ScrapeOutcome)
        self.assertEqual(outcome.status_code, 504)
        self.assertIsInstance(outcome.body, ScrapeError)
        assert isinstance(outcome.body, ScrapeError)
        self.assertEqual(outcome.body.error_category, ErrorCategory.TIMEOUT)
        self.assertEqual(outcome.body.diagnostics.timeout_phase, TimeoutPhase.QUEUE)
        self.assertEqual(outcome.body.error, "Scraper at capacity; retry shortly")
        service.executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()

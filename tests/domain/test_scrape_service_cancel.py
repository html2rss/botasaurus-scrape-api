"""ScrapeService outer timeout maps lease TimeoutError to 504 envelope."""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from app.config import get_settings
from app.domain.scrape_service import ScrapeOutcome, ScrapeService
from app.engine import ScraperEngine
from app.engine.envelope import TIMEOUT_ERROR_BY_PHASE
from app.schemas.enums import ErrorCategory, TimeoutPhase
from app.schemas.response import ScrapeDiagnostics, ScrapeError
from tests.support.factories import scrape_request


class ScrapeServiceLeaseTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_returns_lease_timeout_envelope(self) -> None:
        settings = get_settings()
        engine = ScraperEngine(settings=settings)
        service = ScrapeService(
            settings=settings,
            engine=engine,
            executor=ThreadPoolExecutor(max_workers=1),
        )

        async def boom_run(*, host: str, work: object) -> None:
            del host, work
            raise TimeoutError

        with patch("app.domain.scrape_service.WorkLease") as lease_cls:
            lease = MagicMock()
            lease.run = boom_run
            lease.timeout_error = MagicMock(
                return_value=ScrapeError(
                    url="https://example.com",
                    error=TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.QUEUE],
                    error_category=ErrorCategory.TIMEOUT,
                    diagnostics=ScrapeDiagnostics(
                        request_id="req-timeout",
                        attempts=0,
                        timeout_phase=TimeoutPhase.QUEUE,
                    ),
                )
            )
            lease.snapshot = MagicMock(return_value=MagicMock(warm_hit=None))
            lease_cls.return_value = lease

            outcome = await service.process(scrape_request())

        self.assertIsInstance(outcome, ScrapeOutcome)
        self.assertEqual(outcome.status_code, 504)
        self.assertIsInstance(outcome.body, ScrapeError)
        assert isinstance(outcome.body, ScrapeError)
        self.assertEqual(outcome.body.error_category, ErrorCategory.TIMEOUT)
        self.assertEqual(outcome.body.diagnostics.timeout_phase, TimeoutPhase.QUEUE)
        self.assertEqual(outcome.body.error, TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.QUEUE])
        lease.timeout_error.assert_called_once()
        service.executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()

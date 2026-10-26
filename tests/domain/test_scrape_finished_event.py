"""Parity tests for the single scrape_finished terminal log event."""

import io
import json
import logging
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from app.config import get_settings
from app.domain.scrape_service import ScrapeService
from app.engine import ScraperEngine
from app.engine.envelope import TIMEOUT_ERROR_BY_PHASE
from app.engine.work_lease import LeaseSnapshot
from app.logging_config import LOGGER_NAME, JsonFormatter, TextFormatter, setup_logging
from app.schemas.enums import ErrorCategory, ExecutionTier, TimeoutPhase
from app.schemas.response import ScrapeDiagnostics, ScrapeError, ScrapeSuccess
from tests.support.factories import scrape_request


def _capture_logger(fmt: str) -> tuple[logging.Logger, io.StringIO, logging.Handler]:
    setup_logging(log_format=fmt)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(handler)
    return logger, stream, handler


class ScrapeFinishedEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = get_settings()
        self.engine = ScraperEngine(settings=self.settings)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.service = ScrapeService(
            settings=self.settings,
            engine=self.engine,
            executor=self.executor,
        )

    async def asyncTearDown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    async def test_success_emits_scrape_finished_text_and_json(self) -> None:
        success = ScrapeSuccess(
            url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            html="<html>ok</html>",
            diagnostics=ScrapeDiagnostics(
                request_id="req-ok",
                attempts=1,
                render_ms=42,
                execution_tier=ExecutionTier.HTTP_REQUEST,
            ),
        )

        async def ok_run(*, host: str, work: object) -> ScrapeSuccess:
            del host, work
            return success

        for fmt in ("text", "json"):
            logger, stream, handler = _capture_logger(fmt)
            try:
                with patch("app.domain.scrape_service.WorkLease") as lease_cls:
                    lease = MagicMock()
                    lease.run = ok_run
                    lease.snapshot = MagicMock(
                        return_value=LeaseSnapshot(warm_hit=False)
                    )
                    lease_cls.return_value = lease
                    outcome = await self.service.process(
                        scrape_request(execution_mode="request")
                    )
                self.assertEqual(outcome.status_code, 200)
                output = stream.getvalue()
                self.assertIn("scrape_finished", output)
                self.assertNotIn("scrape_complete", output)
                self.assertNotIn("scrape_timeout", output)
                if fmt == "json":
                    line = output.strip().splitlines()[-1]
                    payload = json.loads(line)
                    self.assertEqual(payload["event_name"], "scrape_finished")
                    self.assertEqual(payload["request_id"], "req-ok")
                    self.assertEqual(payload["status"], 200)
                    self.assertEqual(payload["tier"], "http_request")
                    self.assertEqual(payload["execution_mode"], "request")
                    self.assertIsNone(payload["error_category"])
                    self.assertEqual(payload["warm_hit"], False)
                    self.assertEqual(payload["render_ms"], 42)
                    self.assertIn("duration_ms", payload)
                else:
                    self.assertIn("request_id=req-ok", output)
                    self.assertIn("status=200", output)
                    self.assertIn("tier=http_request", output)
            finally:
                logger.removeHandler(handler)

    async def test_timeout_logs_warning_with_phase_and_request_id(self) -> None:
        timeout_body = ScrapeError(
            url="https://example.com",
            error=TIMEOUT_ERROR_BY_PHASE[TimeoutPhase.BOOT],
            error_category=ErrorCategory.TIMEOUT,
            diagnostics=ScrapeDiagnostics(
                request_id="req-boot-to",
                attempts=0,
                render_ms=100,
                execution_tier=ExecutionTier.BROWSER_DRIVER,
                timeout_phase=TimeoutPhase.BOOT,
            ),
        )

        async def boom_run(*, host: str, work: object) -> None:
            del host, work
            raise TimeoutError

        logger, stream, handler = _capture_logger("json")
        try:
            with (
                patch("app.domain.scrape_service.WorkLease") as lease_cls,
                patch("app.domain.scrape_service.emit_terminal_telemetry"),
            ):
                lease = MagicMock()
                lease.run = boom_run
                lease.timeout_error = MagicMock(return_value=timeout_body)
                lease.snapshot = MagicMock(
                    return_value=LeaseSnapshot(
                        phase=TimeoutPhase.BOOT,
                        warm_hit=True,
                        execution_tier=ExecutionTier.BROWSER_DRIVER,
                    )
                )
                lease_cls.return_value = lease
                outcome = await self.service.process(
                    scrape_request(execution_mode="browser")
                )
            self.assertEqual(outcome.status_code, 504)
            line = stream.getvalue().strip().splitlines()[-1]
            payload = json.loads(line)
            self.assertEqual(payload["event_name"], "scrape_finished")
            self.assertEqual(payload["level"], "warning")
            self.assertEqual(payload["request_id"], "req-boot-to")
            self.assertEqual(payload["timeout_phase"], "boot")
            self.assertEqual(payload["error_category"], "timeout")
            self.assertEqual(payload["status"], 504)
            self.assertEqual(payload["warm_hit"], True)
            self.assertNotIn("scrape_timeout", stream.getvalue())
        finally:
            logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()

"""browser_boot_failed is emitted when Driver construction fails."""

from __future__ import annotations

import io
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import get_settings
from app.engine.browser_tier import run_browser_tier
from app.engine.orchestrator import ScraperEngine
from app.engine.session import ScrapeSession
from app.engine.work_lease import WorkLease
from app.logging_config import LOGGER_NAME, JsonFormatter, setup_logging
from app.schemas.enums import ErrorCategory, TimeoutPhase
from app.schemas.response import ScrapeError
from tests.support.factories import scrape_request


class BrowserBootFailedTests(unittest.TestCase):
    def test_driver_construction_failure_logs_browser_boot_failed(self) -> None:
        setup_logging(log_format="json")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logging.getLogger(LOGGER_NAME).addHandler(handler)

        settings = get_settings()
        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=settings, runtime_root=Path(tmp))
            lease = WorkLease.tracking_only(settings=settings)
            lease.mark(TimeoutPhase.BOOT)
            payload = scrape_request(execution_mode="browser", headless=True)
            session = ScrapeSession(
                engine=engine,
                request_id="req-boot-fail",
                lease=lease,
            )
            started = time.monotonic()
            try:
                with (
                    patch(
                        "app.engine.browser_tier.create_driver_from_fingerprint",
                        side_effect=RuntimeError("Failed to connect to Chrome URL"),
                    ),
                    patch(
                        "app.engine.browser_tier.home_is_writable",
                        return_value=True,
                    ),
                    patch(
                        "app.engine.browser_tier.tmp_free_bytes",
                        return_value=123456,
                    ),
                    session,
                ):
                    result = run_browser_tier(
                        payload,
                        session,
                        started,
                        lease,
                        settings=settings,
                    )
            finally:
                logging.getLogger(LOGGER_NAME).removeHandler(handler)

        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        self.assertEqual(result.error_category, ErrorCategory.NAVIGATION_ERROR)
        lines = [
            json.loads(line)
            for line in stream.getvalue().splitlines()
            if line.strip().startswith("{")
        ]
        boot_events = [
            row for row in lines if row.get("event_name") == "browser_boot_failed"
        ]
        self.assertEqual(len(boot_events), 1)
        event = boot_events[0]
        self.assertEqual(event["request_id"], "req-boot-fail")
        self.assertEqual(event["warm"], False)
        self.assertEqual(event["home_writable"], True)
        self.assertEqual(event["tmp_free_bytes"], 123456)
        self.assertIn("Failed to connect to Chrome URL", event["error"])
        self.assertEqual(event["level"], "warning")


if __name__ == "__main__":
    unittest.main()

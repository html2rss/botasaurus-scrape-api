"""Tests for structured log_event text/JSON parity and health filtering."""

from __future__ import annotations

import io
import json
import logging
import unittest
from unittest.mock import patch

from app.config import LogFormat, Settings, reset_settings_cache
from app.logging_config import (
    LOGGER_NAME,
    HealthAccessFilter,
    JsonFormatter,
    log_event,
    setup_logging,
)


class LoggingConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_settings_cache()
        root = logging.getLogger()
        root.handlers.clear()
        for name in (LOGGER_NAME, "uvicorn", "uvicorn.access", "uvicorn.error"):
            logging.getLogger(name).handlers.clear()
            logging.getLogger(name).filters.clear()

    def test_log_event_text_preserves_kv_message(self) -> None:
        stream = io.StringIO()
        setup_logging(log_format="text")
        logger = logging.getLogger(LOGGER_NAME)
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        try:
            log_event(
                logger,
                logging.INFO,
                "scrape_boot",
                request_id="abc",
                warm_hit=False,
                boot_ms=12,
            )
        finally:
            logger.removeHandler(handler)
        self.assertIn(
            "scrape_boot request_id=abc warm_hit=False boot_ms=12",
            stream.getvalue(),
        )

    def test_log_event_json_emits_one_object_with_fields(self) -> None:
        stream = io.StringIO()
        setup_logging(log_format="json")
        root = logging.getLogger()
        root.handlers.clear()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
        logger = logging.getLogger(LOGGER_NAME)
        try:
            log_event(
                logger,
                logging.WARNING,
                "scrape_finished",
                request_id="rid-1",
                status=504,
                timeout_phase="boot",
            )
        finally:
            root.removeHandler(handler)
        line = stream.getvalue().strip().splitlines()[-1]
        payload = json.loads(line)
        self.assertEqual(payload["event_name"], "scrape_finished")
        self.assertEqual(payload["level"], "warning")
        self.assertEqual(payload["request_id"], "rid-1")
        self.assertEqual(payload["status"], 504)
        self.assertEqual(payload["timeout_phase"], "boot")
        self.assertIn("scrape_finished request_id=rid-1", payload["msg"])
        self.assertNotIn("?", payload["msg"])

    def test_health_access_filter_drops_get_health(self) -> None:
        filt = HealthAccessFilter()
        kept = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s" %s',
            args=("127.0.0.1:1", "GET /scrape HTTP/1.1", 200),
            exc_info=None,
        )
        dropped = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s" %s',
            args=("127.0.0.1:1", "GET /health HTTP/1.1", 200),
            exc_info=None,
        )
        self.assertTrue(filt.filter(kept))
        self.assertFalse(filt.filter(dropped))

    def test_settings_log_format_defaults_to_text(self) -> None:
        reset_settings_cache()
        with patch.dict("os.environ", {}, clear=False):
            # Clear only LOG_FORMAT if present
            import os

            os.environ.pop("LOG_FORMAT", None)
            reset_settings_cache()
            settings = Settings()
            self.assertEqual(settings.log_format, LogFormat.TEXT)

    def test_settings_log_format_json_from_env(self) -> None:
        reset_settings_cache()
        with patch.dict("os.environ", {"LOG_FORMAT": "json"}):
            reset_settings_cache()
            settings = Settings()
            self.assertEqual(settings.log_format, LogFormat.JSON)

    def test_settings_log_format_invalid_falls_back_to_text(self) -> None:
        reset_settings_cache()
        with patch.dict("os.environ", {"LOG_FORMAT": "yaml"}):
            reset_settings_cache()
            settings = Settings()
            self.assertEqual(settings.log_format, LogFormat.TEXT)


if __name__ == "__main__":
    unittest.main()

"""Tests for structured log_event text/JSON parity and health filtering."""

import io
import json
import logging
import sys
import unittest
from unittest.mock import patch

from app.config import LogFormat, Settings, reset_settings_cache
from app.logging_config import (
    LOGGER_NAME,
    JsonFormatter,
    WebsocketTeardownFilter,
    log_event,
    request_log_context,
    setup_logging,
)


class LoggingConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_settings_cache()
        root = logging.getLogger()
        root.handlers.clear()
        for name in (
            LOGGER_NAME,
            "uvicorn",
            "uvicorn.access",
            "uvicorn.error",
            "websocket",
        ):
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
        self.assertNotIn("msg", payload)

    def test_json_omits_none_fields_and_keeps_msg_for_plain_records(self) -> None:
        record = logging.LogRecord(
            "t", logging.INFO, __file__, 1, "plain %s", ("x",), None
        )
        self.assertEqual(json.loads(JsonFormatter().format(record))["msg"], "plain x")
        event = logging.LogRecord("t", logging.INFO, __file__, 1, "e", (), None)
        event.event_name = "scrape_finished"  # type: ignore[attr-defined]
        event.event_fields = {"status": 200, "error_category": None}  # type: ignore[attr-defined]
        payload = json.loads(JsonFormatter().format(event))
        self.assertNotIn("error_category", payload)
        self.assertEqual(payload["status"], 200)

    def test_request_context_attaches_ids_and_explicit_fields_win(self) -> None:
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "e", (), None)
        record.event_name = "scrape_boot"  # type: ignore[attr-defined]
        record.event_fields = {"host": "explicit.example"}  # type: ignore[attr-defined]
        with request_log_context(request_id="rid-9", host="ctx.example"):
            payload = json.loads(JsonFormatter().format(record))
        self.assertEqual(payload["request_id"], "rid-9")
        self.assertEqual(payload["host"], "explicit.example")
        self.assertNotIn("request_id", json.loads(JsonFormatter().format(record)))

    def test_exception_is_structured_not_dropped(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                "t", logging.ERROR, __file__, 1, "e", (), sys.exc_info()
            )
        record.event_name = "unhandled_exception"  # type: ignore[attr-defined]
        record.event_fields = {}  # type: ignore[attr-defined]
        payload = json.loads(JsonFormatter().format(record))
        self.assertEqual(payload["exc_type"], "ValueError")
        self.assertEqual(payload["exc_message"], "boom")
        self.assertIn("Traceback", payload["exc_stack"])

    def test_websocket_teardown_filter_drops_all_websocket_logger_records(self) -> None:
        # Mirrors Sentry before_send: drop every record on logger name "websocket",
        # not only Chromium TTL-reap goodbye lines.
        filt = WebsocketTeardownFilter()
        for msg in (
            "Connection to remote host was lost. - goodbye",
            "Websocket connected",
            "unexpected websocket diagnostic",
        ):
            record = logging.LogRecord(
                name="websocket",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg=msg,
                args=(),
                exc_info=None,
            )
            self.assertFalse(filt.filter(record), msg=msg)

    def test_setup_logging_attaches_websocket_filter(self) -> None:
        setup_logging(log_format="text")
        websocket_logger = logging.getLogger("websocket")
        self.assertTrue(
            any(
                isinstance(f, WebsocketTeardownFilter) for f in websocket_logger.filters
            )
        )
        stream = io.StringIO()
        root = logging.getLogger()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        try:
            websocket_logger.error("Connection to remote host was lost. - goodbye")
            websocket_logger.error("unexpected websocket diagnostic")
        finally:
            root.removeHandler(handler)
        out = stream.getvalue()
        self.assertNotIn("goodbye", out)
        self.assertNotIn("unexpected websocket diagnostic", out)

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

    def test_app_has_no_info_warning_event_format_strings(self) -> None:
        """Ratchet: info/warning events must use log_event so JSON gets event_name."""
        import re
        from pathlib import Path

        app_root = Path(__file__).resolve().parents[2] / "app"
        pattern = re.compile(r'logger\.(?:info|warning)\(\s*\n?\s*["\'][a-z0-9_]+')
        offenders: list[str] = []
        for path in app_root.rglob("*.py"):
            text = path.read_text()
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(app_root.parent)}:{line_no}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

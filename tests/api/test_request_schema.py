"""Unit tests for ScrapeRequest and WindowSize Pydantic schema validation."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.config import get_settings
from app.schemas.enums import ExecutionMode, NavigationMode
from app.schemas.request import WindowSize
from tests.support.factories import scrape_request


class RequestSchemaTests(unittest.TestCase):
    def test_request_defaults(self) -> None:
        payload = scrape_request()
        self.assertEqual(payload.execution_mode, ExecutionMode.AUTO)
        self.assertEqual(payload.navigation_mode, NavigationMode.AUTO)
        self.assertEqual(payload.max_retries, 2)
        self.assertEqual(payload.wait_timeout_seconds, 15)
        self.assertFalse(payload.scroll)
        self.assertTrue(payload.block_images)
        self.assertFalse(payload.block_images_and_css)
        self.assertTrue(payload.block_trackers)
        self.assertTrue(payload.wait_for_complete_page_load)
        self.assertIsNone(payload.user_agent)
        self.assertIsNone(payload.headers)
        self.assertIsNone(payload.cookies)
        self.assertIsNone(payload.window_size)
        self.assertIsNone(payload.lang)
        self.assertFalse(payload.headless)
        self.assertIsNone(payload.proxy)

    def test_scroll_parameters(self) -> None:
        req_scroll = scrape_request(scroll=True)
        self.assertTrue(req_scroll.scroll)
        self.assertFalse(scrape_request().scroll)

    def test_window_size_validation_requires_object(self) -> None:
        with self.assertRaises(ValidationError):
            scrape_request(window_size=[1920, 1080])
        with self.assertRaises(ValidationError):
            scrape_request(window_size={"width": 1920})

    def test_window_size_model_instantiation(self) -> None:
        ws = WindowSize(width=1280, height=720)
        self.assertEqual(ws.width, 1280)
        self.assertEqual(ws.height, 720)

    def test_wait_timeout_seconds_clamps_above_work_cap(self) -> None:
        with self.assertLogs("botasaurus_scrape_api", level="INFO") as captured:
            payload = scrape_request(wait_timeout_seconds=35)

        settings = get_settings()
        self.assertEqual(
            payload.wait_timeout_seconds, settings.scrape_work_timeout_seconds
        )
        self.assertEqual(settings.scrape_work_timeout_seconds, 30)
        self.assertEqual(settings.scrape_timeout_seconds, 45)
        log_text = "\n".join(captured.output)
        self.assertIn("host=example.com", log_text)
        self.assertIn("field=wait_timeout_seconds", log_text)
        self.assertIn("from=35", log_text)
        self.assertIn("to=30", log_text)

    def test_wait_timeout_seconds_clamps_below_one(self) -> None:
        with self.assertLogs("botasaurus_scrape_api", level="INFO") as captured:
            payload = scrape_request(wait_timeout_seconds=0)

        self.assertEqual(payload.wait_timeout_seconds, 1)
        log_text = "\n".join(captured.output)
        self.assertIn("field=wait_timeout_seconds", log_text)
        self.assertIn("from=0", log_text)
        self.assertIn("to=1", log_text)

    def test_clamped_wait_timeout_preserves_clamped_value(self) -> None:
        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            wait_timeout_seconds=35,
        )
        self.assertEqual(
            payload.wait_timeout_seconds, get_settings().scrape_work_timeout_seconds
        )


if __name__ == "__main__":
    unittest.main()

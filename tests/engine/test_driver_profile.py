"""Driver construction keeps Chromium's profile inside the request runtime dir."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import get_settings
from app.engine import ScraperEngine
from app.engine.warm_pool import DriverFingerprint, create_driver_from_fingerprint
from app.schemas.response import ScrapeSuccess
from tests.support.factories import scrape_request
from tests.support.fakes import CaptureDriver


class DriverProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        CaptureDriver.last_init_kwargs = None

    def test_create_driver_from_fingerprint_disables_tiny_profile(self) -> None:
        payload = scrape_request(
            execution_mode="browser",
            headless=True,
            block_images=True,
            lang="en-US",
            window_size={"width": 1280, "height": 720},
        )
        fingerprint = DriverFingerprint.from_request(payload)
        profile_dir = Path("/tmp/scrape/req-profile/profile")

        with patch("botasaurus.browser.Driver", CaptureDriver):
            driver = create_driver_from_fingerprint(fingerprint, profile_dir)

        self.assertIsInstance(driver, CaptureDriver)
        kwargs = CaptureDriver.last_init_kwargs
        assert kwargs is not None
        self.assertEqual(kwargs["profile"], str(profile_dir))
        self.assertIs(kwargs["tiny_profile"], False)
        self.assertEqual(kwargs["headless"], fingerprint.headless)
        self.assertEqual(kwargs["block_images"], fingerprint.block_images)
        self.assertEqual(kwargs["lang"], fingerprint.lang)
        self.assertEqual(kwargs["window_size"], [1280, 720])
        self.assertIs(kwargs["remove_default_browser_check_argument"], True)

    def test_browser_cold_boot_uses_fingerprint_builder(self) -> None:
        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            headless=True,
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            engine = ScraperEngine(settings=get_settings(), runtime_root=runtime_root)
            with patch("botasaurus.browser.Driver", CaptureDriver):
                result = engine.execute(payload, request_id="req-profile")

        self.assertIsInstance(result, ScrapeSuccess)
        kwargs = CaptureDriver.last_init_kwargs
        assert kwargs is not None
        self.assertIs(kwargs["tiny_profile"], False)
        profile = Path(str(kwargs["profile"]))
        self.assertTrue(str(profile).startswith(str(runtime_root)))
        self.assertIn("req-profile", str(profile))


if __name__ == "__main__":
    unittest.main()

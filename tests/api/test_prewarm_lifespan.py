"""App-factory lifespan coverage for SCRAPE_PREWARM flag attach."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from app.config import Settings
from app.engine import ScraperEngine
from app.engine.warm_pool import WarmDriverPool
from app.schemas.response import ScrapeSuccess
from tests.support.factories import scrape_request
from tests.support.fakes import TrackingDriver
from tests.support.http import test_client


def _settings(**env: Any) -> Settings:
    """Build Settings via env aliases (validation_alias, not field names)."""
    return Settings(**env)


class PrewarmLifespanTests(unittest.TestCase):
    def setUp(self) -> None:
        TrackingDriver.reset()

    def test_prewarm_false_leaves_warm_pool_none(self) -> None:
        settings = _settings(SCRAPE_PREWARM=False)
        with test_client(settings=settings) as client:
            engine = client.app.state.engine
            assert isinstance(engine, ScraperEngine)
            self.assertIsNone(engine.warm_pool)
            payload = scrape_request(
                execution_mode="browser",
                navigation_mode="get",
                max_retries=0,
            )
            with patch("botasaurus.browser.Driver", TrackingDriver):
                result = engine.execute(payload)
            self.assertIsInstance(result, ScrapeSuccess)
            self.assertTrue(TrackingDriver.instances[0].closed)

    def test_prewarm_true_attaches_pool_and_shuts_down(self) -> None:
        settings = _settings(SCRAPE_PREWARM=True)
        with patch.object(WarmDriverPool, "shutdown", autospec=True) as mock_shutdown:
            with test_client(settings=settings) as client:
                engine = client.app.state.engine
                assert isinstance(engine, ScraperEngine)
                pool = engine.warm_pool
                self.assertIsInstance(pool, WarmDriverPool)
                assert isinstance(pool, WarmDriverPool)
                self.assertEqual(pool.ready_spare_dirs(), set())
                self.assertEqual(pool.live_spare_dirs(), set())
                # No notify → no Chromium boot during this test.
            mock_shutdown.assert_called_once_with(pool)


class ParseScrapePrewarmTests(unittest.TestCase):
    def test_truthy_env_strings(self) -> None:
        for raw in ("true", "TRUE", "1", "yes", "on", " On "):
            with self.subTest(raw=raw):
                self.assertTrue(_settings(SCRAPE_PREWARM=raw).scrape_prewarm)

    def test_falsy_env_strings(self) -> None:
        for raw in ("false", "0", "no", "off", "", "maybe"):
            with self.subTest(raw=raw):
                self.assertFalse(_settings(SCRAPE_PREWARM=raw).scrape_prewarm)


if __name__ == "__main__":
    unittest.main()

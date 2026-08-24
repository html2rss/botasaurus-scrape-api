import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import get_settings
from app.engine import (
    ScraperEngine,
)
from app.engine.strategies import (
    wait_for_readiness,
)
from app.exceptions import RequestIdCollisionError
from app.schemas.enums import (
    ErrorCategory,
    ExecutionTier,
    NavigationMode,
)
from app.schemas.request import ScrapeRequest
from app.schemas.response import ScrapeDiagnostics, ScrapeError, ScrapeSuccess
from tests.support.fakes import (
    FakeDriver,
)


class ScraperEngineUnitTests(unittest.TestCase):
    def test_browser_tier_step_budget_is_boot_aware(self):
        captured: dict[str, int | None] = {"navigate_timeout": None}

        class _NavigateCaptureDriver(FakeDriver):
            def get(self, *_args, **kwargs):
                captured["navigate_timeout"] = kwargs.get("timeout")
                return None

        payload = ScrapeRequest(
            url="https://example.com",
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
        )

        monotonic_values = [
            1000.0,  # execute started
            1020.0,  # browser ready after boot
            1020.0,  # remaining total
            1020.0,  # remaining work
            1020.0,  # render_ms
        ]

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with (
                patch("botasaurus.browser.Driver", _NavigateCaptureDriver),
                patch(
                    "app.engine.browser_tier.time.monotonic",
                    side_effect=monotonic_values,
                ),
            ):
                result = engine.execute(payload)

        self.assertIsInstance(result, ScrapeSuccess)
        self.assertEqual(captured["navigate_timeout"], 25)

    def test_request_id_collision_raises(self):
        engine = ScraperEngine(settings=get_settings())
        engine.register_request_id("req-123")
        with self.assertRaises(RequestIdCollisionError):
            engine.register_request_id("req-123")
        engine.unregister_request_id("req-123")
        # Should be re-registerable after unregistering
        engine.register_request_id("req-123")
        engine.unregister_request_id("req-123")

    def test_scrape_session_context_manager(self):
        from app.engine import ScrapeSession

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with ScrapeSession(engine, "req-session-1") as session:
                self.assertIn("req-session-1", engine._active_request_ids)
                session.prepare_profile_dirs()
                self.assertTrue(session.profile_dir.is_dir())

            self.assertNotIn("req-session-1", engine._active_request_ids)
            self.assertFalse(session.runtime_dir.exists())

    def test_effective_user_agent_resolution(self):
        req1 = ScrapeRequest(
            url="https://example.com",
            user_agent="CustomAgent/1.0",
            headers={"User-Agent": "HeaderAgent/1.0"},
        )
        self.assertEqual(req1.effective_user_agent, "CustomAgent/1.0")

        req2 = ScrapeRequest(
            url="https://example.com",
            headers={"User-Agent": "HeaderAgent/1.0"},
        )
        self.assertEqual(req2.effective_user_agent, "HeaderAgent/1.0")

        req3 = ScrapeRequest(url="https://example.com")
        self.assertIsNone(req3.effective_user_agent)

    def test_scrape_envelope_constructors(self):
        success = ScrapeSuccess(
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            html="<html></html>",
            metadata_error=None,
            xhr_responses=[],
            diagnostics=ScrapeDiagnostics(
                request_id="req-abc",
                attempts=1,
                strategy_used=NavigationMode.GET,
                render_ms=120,
                execution_tier=ExecutionTier.BROWSER_DRIVER,
            ),
        )
        dumped = success.model_dump(mode="json")
        self.assertEqual(dumped["status_code"], 200)
        self.assertNotIn("error", dumped)
        self.assertEqual(dumped["diagnostics"]["execution_tier"], "browser_driver")
        self.assertEqual(dumped["final_url"], "https://example.com")
        self.assertEqual(dumped["xhr_responses"], [])
        self.assertEqual(dumped["headers"]["content-type"], "text/html; charset=utf-8")

        with_xhr = ScrapeSuccess(
            url="https://example.com",
            html="<html></html>",
            xhr_responses=[
                {
                    "url": "https://api.example.com/items",
                    "status_code": 200,
                    "headers": {"content-type": "application/json"},
                    "body": '{"items":[]}',
                }
            ],
            diagnostics=ScrapeDiagnostics(
                request_id="req-xhr",
                attempts=1,
                strategy_used=NavigationMode.GET,
                render_ms=10,
                execution_tier=ExecutionTier.BROWSER_DRIVER,
            ),
        )
        self.assertEqual(len(with_xhr.xhr_responses), 1)
        self.assertEqual(with_xhr.xhr_responses[0].url, "https://api.example.com/items")

        err = ScrapeError(
            url="https://example.com",
            error="Something broke",
            error_category=ErrorCategory.NAVIGATION_ERROR,
            diagnostics=ScrapeDiagnostics(request_id="req-err"),
        )
        err_dump = err.model_dump(mode="json")
        self.assertEqual(err_dump["error"], "Something broke")
        self.assertEqual(err_dump["error_category"], "navigation_error")
        self.assertNotIn("html", err_dump)
        self.assertNotIn("xhr_responses", err_dump)

    def test_wait_for_readiness_uses_sleep_random_when_available(self):
        mock_driver = MagicMock()
        mock_driver.sleep_random = MagicMock()
        wait_for_readiness(mock_driver, selector=None, timeout_seconds=10)
        mock_driver.sleep_random.assert_called_once_with(0.5, 1.2)

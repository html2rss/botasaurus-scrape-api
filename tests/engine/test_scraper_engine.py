import tempfile
import unittest
from pathlib import Path
from typing import Any
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
    TimeoutPhase,
)
from app.schemas.response import (
    ScrapeDiagnostics,
    ScrapeError,
    ScrapeSuccess,
    XhrResponse,
)
from tests.support.factories import scrape_request
from tests.support.fakes import (
    FakeDriver,
)


class ScraperEngineUnitTests(unittest.TestCase):
    def test_browser_tier_step_budget_is_boot_aware(self):
        captured: dict[str, int | None] = {"navigate_timeout": None}

        class _NavigateCaptureDriver(FakeDriver):
            def get(self, *_args: object, **kwargs: Any) -> None:
                captured["navigate_timeout"] = kwargs.get("timeout")
                return None

        payload = scrape_request(
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
                with self.assertRaises(RequestIdCollisionError):
                    engine.register_request_id("req-session-1")
                session.prepare_profile_dirs()
                self.assertTrue(session.profile_dir.is_dir())

            engine.register_request_id("req-session-1")
            engine.unregister_request_id("req-session-1")
            self.assertFalse(session.runtime_dir.exists())

    def test_effective_user_agent_resolution(self):
        req1 = scrape_request(
            user_agent="CustomAgent/1.0",
            headers={"User-Agent": "HeaderAgent/1.0"},
        )
        self.assertEqual(req1.effective_user_agent, "CustomAgent/1.0")

        req2 = scrape_request(
            headers={"User-Agent": "HeaderAgent/1.0"},
        )
        self.assertEqual(req2.effective_user_agent, "HeaderAgent/1.0")

        req3 = scrape_request()
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
                XhrResponse(
                    url="https://api.example.com/items",
                    status_code=200,
                    headers={"content-type": "application/json"},
                    body='{"items":[]}',
                )
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

    def test_execute_honors_submission_deadline_after_queue_wait(self):
        settings = get_settings()
        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
        )
        submit_at = 1000.0
        deadline = submit_at + settings.scrape_timeout_seconds
        worker_start = submit_at + 30.0
        captured: dict[str, float] = {}

        def fake_browser_tier(
            _payload: object,
            _session: object,
            started_monotonic: float,
            _progress: object,
            *,
            settings: object,
        ) -> ScrapeSuccess:
            del settings
            captured["started_monotonic"] = started_monotonic
            return ScrapeSuccess(
                url="https://example.com",
                html="<html></html>",
                diagnostics=ScrapeDiagnostics(
                    request_id="req-deadline",
                    attempts=1,
                    render_ms=1,
                    execution_tier=ExecutionTier.BROWSER_DRIVER,
                ),
            )

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=settings, runtime_root=Path(tmp))
            with (
                patch(
                    "app.engine.orchestrator.time.monotonic",
                    return_value=worker_start,
                ),
                patch(
                    "app.engine.orchestrator.run_browser_tier",
                    side_effect=fake_browser_tier,
                ),
            ):
                result = engine.execute(payload, deadline_monotonic=deadline)

        self.assertIsInstance(result, ScrapeSuccess)
        self.assertEqual(captured["started_monotonic"], submit_at)

    def test_browser_driver_constructor_failure_returns_navigation_error(self):
        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with patch(
                "botasaurus.browser.Driver",
                side_effect=RuntimeError("chrome binary missing"),
            ):
                result = engine.execute(payload, request_id="req-boot-fail")

        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        self.assertEqual(result.error_category, ErrorCategory.NAVIGATION_ERROR)
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.BOOT)
        self.assertEqual(
            result.diagnostics.execution_tier, ExecutionTier.BROWSER_DRIVER
        )
        self.assertIn("chrome binary missing", result.error)

    def test_cleanup_runs_on_navigation_error(self):
        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            wait_for_selector="#missing",
            wait_timeout_seconds=1,
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            engine = ScraperEngine(settings=get_settings(), runtime_root=runtime_root)
            with patch("botasaurus.browser.Driver", FakeDriver):
                result = engine.execute(payload)

            self.assertIsInstance(result, ScrapeError)
            assert isinstance(result, ScrapeError)
            self.assertEqual(result.error_category, ErrorCategory.NAVIGATION_ERROR)
            self.assertEqual(list(runtime_root.iterdir()), [])

    def test_prepare_profile_dirs_enospc_returns_navigation_error(self):
        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            engine = ScraperEngine(settings=get_settings(), runtime_root=runtime_root)

            def boom_mkdir(
                *_args: object, exist_ok: bool = False, **_kwargs: object
            ) -> None:
                if exist_ok:
                    return None
                raise OSError(28, "No space left on device")

            with (
                patch("botasaurus.browser.Driver", FakeDriver),
                patch.object(Path, "mkdir", side_effect=boom_mkdir),
            ):
                result = engine.execute(payload)

            self.assertIsInstance(result, ScrapeError)
            assert isinstance(result, ScrapeError)
            self.assertEqual(result.error_category, ErrorCategory.NAVIGATION_ERROR)
            self.assertIn("runtime storage full", result.error)
            self.assertIsNotNone(result.diagnostics.timeout_phase)
            assert result.diagnostics.timeout_phase is not None
            self.assertEqual(result.diagnostics.timeout_phase.value, "boot")
            self.assertEqual(list(runtime_root.iterdir()), [])

    def test_prune_orphan_runtime_dirs_before_new_request(self):
        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            orphan = runtime_root / "stale-request"
            orphan.mkdir()
            (orphan / "profile").mkdir()

            engine = ScraperEngine(settings=get_settings(), runtime_root=runtime_root)
            with patch("botasaurus.browser.Driver", FakeDriver):
                result = engine.execute(payload)

            self.assertIsInstance(result, ScrapeSuccess)
            self.assertEqual(
                [entry.name for entry in runtime_root.iterdir()],
                [],
            )

    def test_prune_orphan_runtime_dirs_before_http_request(self):
        from tests.support.fakes import FakeHttpResponse, FakeRequest

        payload = scrape_request(
            execution_mode="request",
        )
        html = "<html><body><h1>Example Domain</h1></body></html>"
        FakeRequest.response = FakeHttpResponse(
            text=html,
            status_code=200,
            headers={"content-type": "text/html"},
            url="https://example.com/",
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            orphan = runtime_root / "stale-request"
            orphan.mkdir()
            (orphan / "profile").mkdir()

            engine = ScraperEngine(settings=get_settings(), runtime_root=runtime_root)
            with patch("botasaurus.request.Request", FakeRequest):
                result = engine.execute(payload)

            self.assertIsInstance(result, ScrapeSuccess)
            assert isinstance(result, ScrapeSuccess)
            self.assertEqual(result.html, html)
            self.assertFalse(orphan.exists())
            self.assertEqual(list(runtime_root.iterdir()), [])

    def test_run_scrape_forwards_driver_kwargs(self):
        from app.schemas.request import WindowSize
        from tests.support.fakes import CaptureDriver

        CaptureDriver.last_init_kwargs = None
        payload = scrape_request(
            execution_mode="browser",
            block_images=True,
            block_images_and_css=True,
            wait_for_complete_page_load=False,
            user_agent="MyAgent/1.0",
            window_size=WindowSize(width=1920, height=1080),
            lang="en-US",
            headless=True,
            proxy="http://proxy.example:8080",
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            engine = ScraperEngine(settings=get_settings(), runtime_root=runtime_root)
            with patch("botasaurus.browser.Driver", CaptureDriver):
                result = engine.execute(payload)

        self.assertIsInstance(result, ScrapeSuccess)
        self.assertIsNotNone(CaptureDriver.last_init_kwargs)
        assert CaptureDriver.last_init_kwargs is not None
        self.assertTrue(CaptureDriver.last_init_kwargs["block_images"])
        self.assertTrue(CaptureDriver.last_init_kwargs["block_images_and_css"])
        self.assertFalse(CaptureDriver.last_init_kwargs["wait_for_complete_page_load"])
        self.assertEqual(CaptureDriver.last_init_kwargs["user_agent"], "MyAgent/1.0")
        self.assertEqual(CaptureDriver.last_init_kwargs["window_size"], [1920, 1080])
        self.assertEqual(CaptureDriver.last_init_kwargs["lang"], "en-US")
        self.assertTrue(CaptureDriver.last_init_kwargs["headless"])
        self.assertEqual(
            CaptureDriver.last_init_kwargs["proxy"], "http://proxy.example:8080"
        )

    def test_request_tier_blocked_status_escalates_to_browser(self):
        from tests.support.fakes import ArticleDriver, FakeHttpResponse, FakeRequest

        payload = scrape_request()
        for status in (401, 403, 429):
            with self.subTest(status=status):
                FakeRequest.response = FakeHttpResponse(
                    text="<html><body>Forbidden</body></html>",
                    status_code=status,
                    headers={"content-type": "text/html"},
                    url="https://example.com/",
                )
                with tempfile.TemporaryDirectory() as tmp:
                    engine = ScraperEngine(
                        settings=get_settings(), runtime_root=Path(tmp)
                    )
                    with (
                        patch("botasaurus.request.Request", FakeRequest),
                        patch("botasaurus.browser.Driver", ArticleDriver),
                    ):
                        result = engine.execute(payload)

                self.assertIsInstance(result, ScrapeSuccess)
                assert isinstance(result, ScrapeSuccess)
                self.assertEqual(
                    result.diagnostics.execution_tier, ExecutionTier.BROWSER_DRIVER
                )
                self.assertIn("<article>", result.html)
                self.assertIn("Headline", result.html)
                self.assertIsNotNone(result.headers)
                assert result.headers is not None
                self.assertEqual(
                    result.headers["content-type"], "text/html; charset=utf-8"
                )

    def test_html_response_sets_utf8_content_type_and_normalizes_body(self):
        from tests.support.fakes import FakeHttpResponse, FakeRequest

        FakeRequest.response = FakeHttpResponse(
            text="<html><body><h1>CaffÃ¨</h1></body></html>",
            status_code=200,
            headers={"content-type": "application/octet-stream"},
            url="https://example.com/",
        )
        payload = scrape_request(
            execution_mode="request",
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with patch("botasaurus.request.Request", FakeRequest):
                result = engine.execute(payload)

        self.assertIsInstance(result, ScrapeSuccess)
        assert isinstance(result, ScrapeSuccess)
        self.assertEqual(result.diagnostics.execution_tier, ExecutionTier.HTTP_REQUEST)
        self.assertIsNotNone(result.headers)
        assert result.headers is not None
        self.assertEqual(result.headers["content-type"], "text/html; charset=utf-8")
        self.assertNotIn("application/octet-stream", result.headers.values())
        self.assertIn("Caffè", result.html)
        self.assertNotIn("CaffÃ¨", result.html)
        result.html.encode("utf-8")


if __name__ == "__main__":
    unittest.main()

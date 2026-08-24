import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from app.config import get_settings
from app.engine import (
    ScraperEngine,
    html_document_headers,
    utf8_normalize_html,
)
from app.engine.strategies import (
    apply_scrolling,
    resolve_strategies,
)
from app.schemas.enums import (
    ErrorCategory,
    ExecutionMode,
    ExecutionTier,
    NavigationMode,
)
from app.schemas.request import ScrapeRequest, WindowSize
from app.schemas.response import ScrapeError, ScrapeSuccess
from tests.support.fakes import (
    ArticleDriver,
    CaptureDriver,
    FakeDriver,
    FakeHttpResponse,
    FakeRequest,
)


class RequestSchemaTests(unittest.TestCase):
    def test_request_defaults(self):
        payload = ScrapeRequest(url="https://example.com")
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

    def test_scroll_parameters(self):
        req_scroll = ScrapeRequest(url="https://example.com", scroll=True)
        self.assertTrue(req_scroll.scroll)
        self.assertFalse(ScrapeRequest(url="https://example.com").scroll)

    def test_window_size_validation_requires_object(self):
        with self.assertRaises(ValidationError):
            ScrapeRequest(url="https://example.com", window_size=[1920, 1080])
        with self.assertRaises(ValidationError):
            ScrapeRequest(url="https://example.com", window_size={"width": 1920})

    def test_wait_timeout_seconds_clamps_above_work_cap(self):
        with self.assertLogs("botasaurus_scrape_api", level="INFO") as captured:
            payload = ScrapeRequest(url="https://example.com", wait_timeout_seconds=35)

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

    def test_wait_timeout_seconds_clamps_below_one(self):
        with self.assertLogs("botasaurus_scrape_api", level="INFO") as captured:
            payload = ScrapeRequest(url="https://example.com", wait_timeout_seconds=0)

        self.assertEqual(payload.wait_timeout_seconds, 1)
        log_text = "\n".join(captured.output)
        self.assertIn("field=wait_timeout_seconds", log_text)
        self.assertIn("from=0", log_text)
        self.assertIn("to=1", log_text)

    def test_clamped_wait_timeout_allows_execute(self):
        payload = ScrapeRequest(
            url="https://example.com",
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            wait_timeout_seconds=35,
        )
        self.assertEqual(
            payload.wait_timeout_seconds, get_settings().scrape_work_timeout_seconds
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with patch("app.engine.browser_tier.Driver", FakeDriver):
                result = engine.execute(payload)

        self.assertIsNone(result.error if isinstance(result, ScrapeError) else None)
        self.assertIsInstance(result, ScrapeSuccess)
        self.assertEqual(
            result.html, "<html><body><h1>Example Domain</h1></body></html>"
        )

    def test_html_response_sets_utf8_content_type_and_normalizes_body(self):
        FakeRequest.response = FakeHttpResponse(
            text="<html><body><h1>CaffÃ¨</h1></body></html>",
            status_code=200,
            headers={"content-type": "application/octet-stream"},
            url="https://example.com/",
        )
        payload = ScrapeRequest(
            url="https://example.com",
            execution_mode="request",
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with patch("app.engine.request_tier.Request", FakeRequest):
                result = engine.execute(payload)

        self.assertIsInstance(result, ScrapeSuccess)
        self.assertEqual(result.diagnostics.execution_tier, ExecutionTier.HTTP_REQUEST)
        self.assertIsNotNone(result.headers)
        self.assertEqual(result.headers["content-type"], "text/html; charset=utf-8")
        self.assertNotIn("application/octet-stream", result.headers.values())
        self.assertIn("Caffè", result.html)
        self.assertNotIn("CaffÃ¨", result.html)
        result.html.encode("utf-8")

    def test_utf8_normalize_leaves_correct_unicode_unchanged(self):
        html = "<html><body><h1>Caffè 日本語</h1></body></html>"
        self.assertEqual(utf8_normalize_html(html), html)

        normalized, headers = html_document_headers(html, {"content-type": "text/html"})
        self.assertEqual(normalized, html)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")

        FakeRequest.response = FakeHttpResponse(
            text=html,
            status_code=200,
            headers={"content-type": "text/html"},
            url="https://example.com/",
        )
        payload = ScrapeRequest(url="https://example.com", execution_mode="request")
        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with patch("app.engine.request_tier.Request", FakeRequest):
                result = engine.execute(payload)

        self.assertEqual(result.html, html)
        self.assertEqual(result.headers["content-type"], "text/html; charset=utf-8")

    def test_request_tier_blocked_status_escalates_to_browser(self):
        payload = ScrapeRequest(url="https://example.com")
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
                        patch("app.engine.request_tier.Request", FakeRequest),
                        patch("app.engine.browser_tier.Driver", ArticleDriver),
                    ):
                        result = engine.execute(payload)

                self.assertIsInstance(result, ScrapeSuccess)
                self.assertEqual(
                    result.diagnostics.execution_tier, ExecutionTier.BROWSER_DRIVER
                )
                self.assertIn("<article>", result.html)
                self.assertIn("Headline", result.html)
                self.assertEqual(
                    result.headers["content-type"], "text/html; charset=utf-8"
                )

    def test_strategy_selection(self):
        self.assertEqual(
            resolve_strategies(NavigationMode.AUTO, 0),
            [NavigationMode.GOOGLE_GET],
        )
        self.assertEqual(
            resolve_strategies(NavigationMode.AUTO, 2),
            [
                NavigationMode.GOOGLE_GET,
                NavigationMode.GOOGLE_GET_BYPASS,
                NavigationMode.GET,
            ],
        )
        self.assertEqual(
            resolve_strategies(NavigationMode.GET, 2),
            [NavigationMode.GET, NavigationMode.GET, NavigationMode.GET],
        )
        self.assertEqual(
            resolve_strategies(NavigationMode.ORGANIC_GET, 2),
            [
                NavigationMode.ORGANIC_GET,
                NavigationMode.ORGANIC_GET,
                NavigationMode.ORGANIC_GET,
            ],
        )

    def test_cleanup_runs_on_navigation_error(self):
        payload = ScrapeRequest(
            url="https://example.com",
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            wait_for_selector="#missing",
            wait_timeout_seconds=1,
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            engine = ScraperEngine(settings=get_settings(), runtime_root=runtime_root)
            with patch("app.engine.browser_tier.Driver", FakeDriver):
                result = engine.execute(payload)

            self.assertEqual(result.error_category, ErrorCategory.NAVIGATION_ERROR)
            self.assertEqual(list(runtime_root.iterdir()), [])

    def test_prepare_profile_dirs_enospc_returns_navigation_error(self):
        payload = ScrapeRequest(
            url="https://example.com",
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            engine = ScraperEngine(settings=get_settings(), runtime_root=runtime_root)

            def boom_mkdir(*_args, exist_ok=False, **_kwargs):
                if exist_ok:
                    return None
                raise OSError(28, "No space left on device")

            with (
                patch("app.engine.browser_tier.Driver", FakeDriver),
                patch.object(Path, "mkdir", side_effect=boom_mkdir),
            ):
                result = engine.execute(payload)

            self.assertEqual(result.error_category, ErrorCategory.NAVIGATION_ERROR)
            self.assertIn("runtime storage full", result.error)
            self.assertEqual(result.diagnostics.timeout_phase.value, "boot")
            self.assertEqual(list(runtime_root.iterdir()), [])

    def test_prune_orphan_runtime_dirs_before_new_request(self):
        payload = ScrapeRequest(
            url="https://example.com",
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
            with patch("app.engine.browser_tier.Driver", FakeDriver):
                result = engine.execute(payload)

            self.assertIsInstance(result, ScrapeSuccess)
            self.assertEqual(
                [entry.name for entry in runtime_root.iterdir()],
                [],
            )

    def test_prune_orphan_runtime_dirs_before_http_request(self):
        payload = ScrapeRequest(
            url="https://example.com",
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
            with patch("app.engine.request_tier.Request", FakeRequest):
                result = engine.execute(payload)

            self.assertEqual(result.html, html)
            self.assertFalse(orphan.exists())
            self.assertEqual(list(runtime_root.iterdir()), [])

    def test_run_scrape_forwards_driver_kwargs(self):
        CaptureDriver.last_init_kwargs = None
        payload = ScrapeRequest(
            url="https://example.com",
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
            with patch("app.engine.browser_tier.Driver", CaptureDriver):
                result = engine.execute(payload)

        self.assertIsInstance(result, ScrapeSuccess)
        self.assertIsNotNone(CaptureDriver.last_init_kwargs)
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

    def test_apply_scrolling(self):
        mock_driver = MagicMock()
        mock_driver.scroll_to_bottom = MagicMock()
        apply_scrolling(mock_driver)
        mock_driver.scroll_to_bottom.assert_called_once()

import ipaddress
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from app.detector import ChallengeDetector
from app.engine import (
    ScraperEngine,
    html_document_headers,
    utf8_normalize_html,
)
from app.metadata import MetadataExtractor
from app.schemas import (
    DEFAULT_SCRAPE_TIMEOUT_SECONDS,
    ErrorCategory,
    ExecutionMode,
    ExecutionTier,
    NavigationMode,
    ScrapeDiagnostics,
    ScrapeError,
    ScrapeRequest,
    ScrapeSuccess,
    WindowSize,
)
from app.security import UrlGuard


class _FakeMetadataResponse:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "text/html"}
    url = "https://example.com/"


class _FakeRequests:
    def get(self, _url):
        return _FakeMetadataResponse()


class _FakeDriver:
    def __init__(self, *args, **kwargs):
        self.page_html = "<html><body><h1>Example Domain</h1></body></html>"
        self.current_url = "https://example.com/"
        self.requests = _FakeRequests()
        self._raise_wait = kwargs.pop("raise_wait", False)
        self.scrolled = False

    def get(self, *_args, **_kwargs):
        return None

    def google_get(self, *_args, **_kwargs):
        return None

    def organic_get(self, *_args, **_kwargs):
        return None

    def wait_for_element(self, *_args, **_kwargs):
        raise RuntimeError("missing selector")

    def scroll_to_bottom(self):
        self.scrolled = True

    def sleep(self, *_args, **_kwargs):
        return None

    def save_screenshot(self, filename):
        Path(filename).write_bytes(b"fake")

    def close(self):
        return None


class _CaptureDriver(_FakeDriver):
    last_init_kwargs = None

    def __init__(self, *args, **kwargs):
        type(self).last_init_kwargs = dict(kwargs)
        super().__init__(*args, **kwargs)


class _FakeHttpResponse:
    def __init__(self, *, text, status_code, headers, url):
        self.text = text
        self.status_code = status_code
        self.headers = headers
        self.url = url


class _FakeRequest:
    response = None

    def get(self, *_args, **_kwargs):
        return type(self).response

    def close(self):
        return None


class _ArticleDriver(_FakeDriver):
    ARTICLE_HTML = (
        "<html><body><article><h1>Headline</h1>"
        "<p>Lead paragraph</p></article></body></html>"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.page_html = self.ARTICLE_HTML


class MainUnitTests(unittest.TestCase):
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

    def test_wait_timeout_seconds_clamps_gem_default_to_service_cap(self):
        with self.assertLogs("botasaurus_scrape_api", level="INFO") as captured:
            payload = ScrapeRequest(url="https://example.com", wait_timeout_seconds=28)

        self.assertEqual(payload.wait_timeout_seconds, DEFAULT_SCRAPE_TIMEOUT_SECONDS)
        self.assertEqual(DEFAULT_SCRAPE_TIMEOUT_SECONDS, 20)
        log_text = "\n".join(captured.output)
        self.assertIn("host=example.com", log_text)
        self.assertIn("field=wait_timeout_seconds", log_text)
        self.assertIn("from=28", log_text)
        self.assertIn("to=20", log_text)

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
            wait_timeout_seconds=28,
        )
        self.assertEqual(payload.wait_timeout_seconds, DEFAULT_SCRAPE_TIMEOUT_SECONDS)

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(runtime_root=Path(tmp))
            with patch("app.engine.Driver", _FakeDriver):
                result = engine.execute(payload)

        self.assertIsNone(result.error if isinstance(result, ScrapeError) else None)
        self.assertIsInstance(result, ScrapeSuccess)
        self.assertEqual(
            result.html, "<html><body><h1>Example Domain</h1></body></html>"
        )

    def test_html_response_sets_utf8_content_type_and_normalizes_body(self):
        _FakeRequest.response = _FakeHttpResponse(
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
            engine = ScraperEngine(runtime_root=Path(tmp))
            with patch("app.engine.Request", _FakeRequest):
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

        _FakeRequest.response = _FakeHttpResponse(
            text=html,
            status_code=200,
            headers={"content-type": "text/html"},
            url="https://example.com/",
        )
        payload = ScrapeRequest(url="https://example.com", execution_mode="request")
        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(runtime_root=Path(tmp))
            with patch("app.engine.Request", _FakeRequest):
                result = engine.execute(payload)

        self.assertEqual(result.html, html)
        self.assertEqual(result.headers["content-type"], "text/html; charset=utf-8")

    def test_request_tier_blocked_status_escalates_to_browser(self):
        payload = ScrapeRequest(url="https://example.com")
        for status in (401, 403, 429):
            with self.subTest(status=status):
                _FakeRequest.response = _FakeHttpResponse(
                    text="<html><body>Forbidden</body></html>",
                    status_code=status,
                    headers={"content-type": "text/html"},
                    url="https://example.com/",
                )
                with tempfile.TemporaryDirectory() as tmp:
                    engine = ScraperEngine(runtime_root=Path(tmp))
                    with (
                        patch("app.engine.Request", _FakeRequest),
                        patch("app.engine.Driver", _ArticleDriver),
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
        self.assertEqual(ScraperEngine.resolve_strategies("auto", 0), ["google_get"])
        self.assertEqual(
            ScraperEngine.resolve_strategies("auto", 2),
            ["google_get", "google_get_bypass", "get"],
        )
        self.assertEqual(
            ScraperEngine.resolve_strategies("get", 2), ["get", "get", "get"]
        )
        self.assertEqual(
            ScraperEngine.resolve_strategies("organic_get", 2),
            ["organic_get", "organic_get", "organic_get"],
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
            engine = ScraperEngine(runtime_root=runtime_root)
            with patch("app.engine.Driver", _FakeDriver):
                result = engine.execute(payload)

            self.assertEqual(result.error_category, ErrorCategory.NAVIGATION_ERROR)
            self.assertEqual(list(runtime_root.iterdir()), [])

    def test_run_scrape_forwards_driver_kwargs(self):
        _CaptureDriver.last_init_kwargs = None
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
            engine = ScraperEngine(runtime_root=runtime_root)
            with patch("app.engine.Driver", _CaptureDriver):
                result = engine.execute(payload)

        self.assertIsInstance(result, ScrapeSuccess)
        self.assertIsNotNone(_CaptureDriver.last_init_kwargs)
        self.assertTrue(_CaptureDriver.last_init_kwargs["block_images"])
        self.assertTrue(_CaptureDriver.last_init_kwargs["block_images_and_css"])
        self.assertFalse(_CaptureDriver.last_init_kwargs["wait_for_complete_page_load"])
        self.assertEqual(_CaptureDriver.last_init_kwargs["user_agent"], "MyAgent/1.0")
        self.assertEqual(_CaptureDriver.last_init_kwargs["window_size"], [1920, 1080])
        self.assertEqual(_CaptureDriver.last_init_kwargs["lang"], "en-US")
        self.assertTrue(_CaptureDriver.last_init_kwargs["headless"])
        self.assertEqual(
            _CaptureDriver.last_init_kwargs["proxy"], "http://proxy.example:8080"
        )

    def test_apply_scrolling(self):
        mock_driver = MagicMock()
        mock_driver.scroll_to_bottom = MagicMock()
        ScraperEngine.apply_scrolling(mock_driver)
        mock_driver.scroll_to_bottom.assert_called_once()


class UrlGuardUnitTests(unittest.TestCase):
    def test_validate_rejects_non_http_schemes(self):
        res = UrlGuard.validate("ftp://example.com/file")
        self.assertFalse(res.is_allowed)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Only http/https", res.error_message)

    def test_validate_rejects_missing_hostname(self):
        res = UrlGuard.validate("http://")
        self.assertFalse(res.is_allowed)
        self.assertEqual(res.status_code, 400)

    def test_validate_rejects_localhost(self):
        res = UrlGuard.validate("http://localhost:8080/test")
        self.assertFalse(res.is_allowed)
        self.assertEqual(res.status_code, 403)

    def test_validate_proxy_rejects_blocked_host(self):
        res = UrlGuard.validate_proxy("http://127.0.0.1:8080")
        self.assertFalse(res.is_allowed)
        self.assertEqual(res.status_code, 403)
        self.assertIn("Proxy URL is invalid or blocked", res.error_message)

    def test_is_blocked_ip_allows_well_known_nat64_prefix(self):
        nat64_ip = ipaddress.ip_address("64:ff9b::3691:8e03")
        self.assertFalse(UrlGuard.is_blocked_ip(nat64_ip))

    def test_is_blocked_ip_still_blocks_loopback(self):
        loopback = ipaddress.ip_address("127.0.0.1")
        self.assertTrue(UrlGuard.is_blocked_ip(loopback))


class ChallengeDetectorUnitTests(unittest.TestCase):
    def test_detects_challenge_marker_and_category(self):
        res = ChallengeDetector.detect("<html>Just a moment...</html>", 200)
        self.assertTrue(res.challenge_detected)
        self.assertTrue(res.blocked_detected)
        self.assertEqual(res.detected_marker, "Just a moment...")
        self.assertEqual(res.error_category, "challenge_block")
        self.assertFalse(res.is_clean)

    def test_detects_http_status_block_without_marker(self):
        res = ChallengeDetector.detect("<html>Forbidden</html>", 403)
        self.assertFalse(res.challenge_detected)
        self.assertTrue(res.blocked_detected)
        self.assertIsNone(res.detected_marker)
        self.assertEqual(res.error_category, "challenge_block")
        self.assertFalse(res.is_clean)

    def test_clean_response(self):
        res = ChallengeDetector.detect("<html><h1>Hello</h1></html>", 200)
        self.assertTrue(res.is_clean)
        self.assertFalse(res.blocked_detected)
        self.assertFalse(res.challenge_detected)
        self.assertIsNone(res.error_category)

    def test_driver_bot_detection_integration(self):
        mock_driver = MagicMock()
        mock_driver.is_bot_detected.return_value = True

        res = ChallengeDetector.detect(
            "<html><body>Clean page</body></html>", 200, driver=mock_driver
        )
        self.assertTrue(res.challenge_detected)
        self.assertTrue(res.blocked_detected)
        self.assertEqual(res.detected_marker, "botasaurus_driver_bot_detected")
        self.assertEqual(res.error_category, "challenge_block")


class MetadataExtractorUnitTests(unittest.TestCase):
    def test_extract_passive_metadata_from_requests_list(self):
        class _Req:
            def __init__(self, status, headers, url):
                self.response = type(
                    "Resp", (), {"status_code": status, "headers": headers}
                )()
                self.url = url

        driver = type(
            "D",
            (),
            {
                "requests": [
                    _Req(
                        200, {"content-type": "text/html"}, "https://example.com/final"
                    )
                ]
            },
        )()
        status, headers, final_url = MetadataExtractor.extract_from_requests(
            driver, "https://example.com"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers, {"content-type": "text/html"})
        self.assertEqual(final_url, "https://example.com/final")

    def test_extract_passive_metadata_from_performance_logs(self):
        import json

        perf_log = [
            {
                "message": json.dumps(
                    {
                        "message": {
                            "method": "Network.responseReceived",
                            "params": {
                                "type": "Document",
                                "response": {
                                    "status": 200,
                                    "headers": {"content-type": "text/html"},
                                    "url": "https://example.com/cdp-final",
                                },
                            },
                        }
                    }
                )
            }
        ]
        driver = type(
            "D",
            (),
            {
                "get_log": lambda self, log_type: (
                    perf_log if log_type == "performance" else []
                )
            },
        )()
        status, headers, final_url = MetadataExtractor.extract_from_cdp_logs(
            driver, "https://example.com"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers, {"content-type": "text/html"})
        self.assertEqual(final_url, "https://example.com/cdp-final")

    def test_extract_falls_back_to_200_when_no_driver_metadata(self):
        driver = type("EmptyDriver", (), {"current_url": "https://example.com/dest"})()
        meta = MetadataExtractor.fetch(driver, "https://example.com")
        self.assertEqual(meta.status_code, 200)
        self.assertEqual(meta.final_url, "https://example.com/dest")
        self.assertIsNone(meta.headers)
        self.assertIsNone(meta.metadata_error)


class ScraperEngineUnitTests(unittest.TestCase):
    def test_request_id_collision_raises(self):
        engine = ScraperEngine()
        engine.register_request_id("req-123")
        with self.assertRaises(RuntimeError):
            engine.register_request_id("req-123")
        engine.unregister_request_id("req-123")
        # Should be re-registerable after unregistering
        engine.register_request_id("req-123")
        engine.unregister_request_id("req-123")

    def test_scrape_session_context_manager(self):
        from app.engine import ScrapeSession

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(runtime_root=Path(tmp))
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
        ScraperEngine.wait_for_readiness(mock_driver, selector=None, timeout_seconds=10)
        mock_driver.sleep_random.assert_called_once_with(0.5, 1.2)


class _FakeRequestId(str):
    def to_json(self):
        return str(self)


class _FakeNetworkResponse:
    def __init__(self, url, status, mime_type, headers=None):
        self.url = url
        self.status = status
        self.mime_type = mime_type
        self.headers = headers or {}


class _FakeTab:
    """Minimal CDP tab stub for XhrCollector unit tests."""

    def __init__(self, bodies=None):
        self.bodies = bodies or {}
        self.network_enabled = False
        self.response_handler = None
        self.finished_handler = None

    def send(self, cdp_obj):
        cmd = next(cdp_obj)
        method = cmd.get("method")
        if method == "Network.enable":
            self.network_enabled = True
            try:
                cdp_obj.send({})
            except StopIteration as exc:
                return exc.value
            return None
        if method == "Network.getResponseBody":
            rid = str(cmd["params"]["requestId"])
            body, b64 = self.bodies.get(rid, ("", False))
            try:
                cdp_obj.send({"body": body, "base64Encoded": b64})
            except StopIteration as exc:
                return exc.value
            return None
        raise AssertionError(f"unexpected CDP method: {method}")

    def after_response_received(self, handler):
        self.response_handler = handler

    def add_handler(self, _event_type, handler):
        self.finished_handler = handler


class XhrCollectorTests(unittest.TestCase):
    def setUp(self):
        from app.xhr_collector import XhrCollector

        self.XhrCollector = XhrCollector
        self.target = "https://example.com/"
        self.collector = XhrCollector(self.target)

    def _drive_json(self, tab, request_id, url, body, mime="application/json"):
        rid = _FakeRequestId(request_id)
        tab.bodies[str(rid)] = (body, False)
        self.collector._on_response(
            rid,
            _FakeNetworkResponse(url, 200, mime, {"content-type": mime}),
            None,
        )
        self.collector._on_finished(type("E", (), {"request_id": rid})())

    def test_install_enables_network_and_registers_handlers(self):
        tab = _FakeTab()
        self.collector.install(tab)
        self.assertTrue(tab.network_enabled)
        self.assertEqual(
            tab.response_handler.__func__, self.collector._on_response.__func__
        )
        self.assertIs(tab.response_handler.__self__, self.collector)
        self.assertEqual(
            tab.finished_handler.__func__, self.collector._on_finished.__func__
        )
        self.assertIs(tab.finished_handler.__self__, self.collector)

    def test_captures_json_subresource(self):
        tab = _FakeTab()
        self.collector.install(tab)
        self._drive_json(
            tab, "1", "https://api.example.com/feed", '{"items":[{"title":"A"}]}'
        )
        results = self.collector.harvest(tab)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://api.example.com/feed")
        self.assertEqual(results[0]["status_code"], 200)
        self.assertEqual(results[0]["headers"], {"content-type": "application/json"})
        self.assertIn("items", results[0]["body"])

    def test_skips_non_json_mime(self):
        tab = _FakeTab()
        self.collector.install(tab)
        self._drive_json(
            tab,
            "1",
            "https://cdn.example.com/app.js",
            "console.log(1)",
            mime="application/javascript",
        )
        self.assertEqual(self.collector.harvest(tab), [])

    def test_skips_main_document(self):
        tab = _FakeTab()
        self.collector.install(tab)
        rid = _FakeRequestId("doc")
        tab.bodies[str(rid)] = ('{"nope":true}', False)
        self.collector._on_response(
            rid,
            _FakeNetworkResponse(self.target, 200, "application/json"),
            None,
        )
        self.collector._on_finished(type("E", (), {"request_id": rid})())
        self.assertEqual(self.collector.harvest(tab), [])

    def test_skips_empty_body(self):
        tab = _FakeTab()
        self.collector.install(tab)
        self._drive_json(tab, "1", "https://api.example.com/empty", "")
        self.assertEqual(self.collector.harvest(tab), [])

    def test_enforces_max_responses_cap(self):
        tab = _FakeTab()
        self.collector.install(tab)
        for i in range(self.XhrCollector.MAX_RESPONSES + 5):
            self._drive_json(
                tab, str(i), f"https://api.example.com/i/{i}", f'{{"i":{i}}}'
            )
        results = self.collector.harvest(tab)
        self.assertEqual(len(results), self.XhrCollector.MAX_RESPONSES)

    def test_enforces_max_body_bytes_cap(self):
        tab = _FakeTab()
        self.collector.install(tab)
        oversized = "x" * (self.XhrCollector.MAX_BODY_BYTES + 1)
        self._drive_json(tab, "1", "https://api.example.com/big", oversized)
        self.assertEqual(self.collector.harvest(tab), [])

    def test_enforces_aggregate_bytes_cap(self):
        tab = _FakeTab()
        self.collector.install(tab)
        # Five near-max bodies would exceed 2 MB aggregate; stop once budget trips.
        chunk = "y" * self.XhrCollector.MAX_BODY_BYTES
        for i in range(5):
            self._drive_json(tab, str(i), f"https://api.example.com/chunk/{i}", chunk)
        results = self.collector.harvest(tab)
        total = sum(len(entry["body"].encode("utf-8")) for entry in results)
        self.assertLessEqual(total, self.XhrCollector.MAX_AGGREGATE_BYTES)
        self.assertEqual(len(results), 4)
        self.assertLess(len(results), 5)

    def test_headers_allowlist_keeps_only_content_type(self):
        tab = _FakeTab()
        self.collector.install(tab)
        rid = _FakeRequestId("hdr")
        tab.bodies[str(rid)] = ('{"ok":true}', False)
        self.collector._on_response(
            rid,
            _FakeNetworkResponse(
                "https://api.example.com/secure",
                200,
                "application/json",
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Set-Cookie": "session=secret",
                    "X-Request-Id": "abc",
                },
            ),
            None,
        )
        self.collector._on_finished(type("E", (), {"request_id": rid})())
        results = self.collector.harvest(tab)
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["headers"],
            {"content-type": "application/json; charset=utf-8"},
        )
        self.assertNotIn("Set-Cookie", results[0]["headers"])
        self.assertNotIn("set-cookie", results[0]["headers"])

    def test_reset_clears_pending_ready_and_collected(self):
        tab = _FakeTab()
        self.collector.install(tab)
        self._drive_json(
            tab, "1", "https://api.example.com/old", '{"from":"failed-attempt"}'
        )
        first = self.collector.harvest(tab)
        self.assertEqual(len(first), 1)

        self.collector.reset()
        self.assertEqual(self.collector.results(), [])

        self._drive_json(
            tab, "2", "https://api.example.com/new", '{"from":"success-attempt"}'
        )
        second = self.collector.harvest(tab)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["body"], '{"from":"success-attempt"}')
        self.assertNotIn("failed-attempt", second[0]["body"])


class SchemaValidationHttpTests(unittest.TestCase):
    def test_schema_422_returns_scrape_envelope(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with self.assertLogs("botasaurus_scrape_api", level="INFO") as captured:
            client = TestClient(app)
            response = client.post(
                "/scrape",
                json={
                    "url": "https://example.com",
                    "window_size": [1920],
                },
            )

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertNotIn("detail", body)
        self.assertEqual(body["url"], "https://example.com")
        self.assertTrue(body["error"])
        self.assertIn("window_size", body["error"])
        self.assertEqual(body["error_category"], "validation")
        self.assertNotIn("html", body)
        self.assertTrue(body["diagnostics"]["request_id"])
        log_text = "\n".join(captured.output)
        self.assertIn("request_schema_422", log_text)
        self.assertIn("host=example.com", log_text)
        self.assertIn("field=window_size", log_text)

    def test_scrape_clamps_wait_timeout_instead_of_422(self):
        from fastapi.testclient import TestClient

        import app.main as main_mod
        from app.main import app

        captured: dict[str, int] = {}

        def fake_execute(payload, _deadline=None):
            captured["wait"] = payload.wait_timeout_seconds
            return ScrapeSuccess(
                url=str(payload.url),
                html="<html></html>",
                diagnostics=ScrapeDiagnostics(
                    request_id="req-wait-clamp",
                    attempts=1,
                    render_ms=1,
                    execution_tier=ExecutionTier.HTTP_REQUEST,
                ),
            )

        with patch.object(main_mod._engine, "execute", side_effect=fake_execute):
            client = TestClient(app)
            response = client.post(
                "/scrape",
                json={
                    "url": "https://example.com",
                    "wait_timeout_seconds": 28,
                },
            )

        self.assertNotEqual(response.status_code, 422)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["wait"], DEFAULT_SCRAPE_TIMEOUT_SECONDS)
        self.assertEqual(captured["wait"], 20)


def _schema_ref_names(node: object) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and "/schemas/" in ref:
            names.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            names |= _schema_ref_names(value)
    elif isinstance(node, list):
        for item in node:
            names |= _schema_ref_names(item)
    return names


class OpenApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.schema = app.openapi()

    def test_documents_health_and_scrape_paths(self):
        paths = self.schema["paths"]
        self.assertIn("/health", paths)
        self.assertIn("get", paths["/health"])
        self.assertIn("/scrape", paths)
        self.assertIn("post", paths["/scrape"])

    def test_operation_ids_and_tags(self):
        health = self.schema["paths"]["/health"]["get"]
        scrape = self.schema["paths"]["/scrape"]["post"]
        self.assertEqual(health["operationId"], "get-health")
        self.assertEqual(scrape["operationId"], "scrape-url")
        self.assertEqual(health["tags"], ["health"])
        self.assertEqual(scrape["tags"], ["scrape"])
        tag_names = {tag["name"] for tag in self.schema["tags"]}
        self.assertEqual(tag_names, {"health", "scrape"})
        for tag in self.schema["tags"]:
            self.assertTrue(tag.get("description"))

    def test_info_servers_and_version(self):
        info = self.schema["info"]
        self.assertEqual(info["title"], "Botasaurus Scrape API")
        self.assertEqual(info["version"], "2.0.0")
        self.assertTrue(info.get("description"))
        self.assertEqual(info["contact"]["name"], "html2rss")
        self.assertEqual(
            info["contact"]["url"],
            "https://github.com/html2rss/botasaurus-scrape-api/issues",
        )
        self.assertNotIn("email", info["contact"])
        self.assertEqual(info["license"]["name"], "MIT")
        self.assertTrue(info["license"].get("url"))
        servers = self.schema["servers"]
        self.assertEqual(servers[0]["url"], "http://localhost:4010")
        self.assertEqual(servers[0]["description"], "Local Docker (make serve)")

    def test_scrape_documents_contract_status_codes(self):
        responses = self.schema["paths"]["/scrape"]["post"]["responses"]
        for status in ("200", "400", "403", "422", "502", "504"):
            self.assertIn(status, responses)
            self.assertTrue(responses[status].get("description"))

    def test_scrape_error_statuses_use_scrape_envelope_not_fastapi_detail(self):
        responses = self.schema["paths"]["/scrape"]["post"]["responses"]
        success_refs = _schema_ref_names(responses["200"])
        self.assertIn("ScrapeSuccess", success_refs)
        self.assertNotIn("ScrapeResponse", success_refs)
        for status in ("400", "403", "422", "502", "504"):
            with self.subTest(status=status):
                refs = _schema_ref_names(responses[status])
                self.assertIn("ScrapeError", refs)
                self.assertNotIn("ScrapeResponse", refs)
                self.assertNotIn("HTTPValidationError", refs)
                self.assertNotIn("ValidationError", refs)

    def test_wait_timeout_seconds_openapi_does_not_advertise_range_as_422(self):
        props = self.schema["components"]["schemas"]["ScrapeRequest"]["properties"]
        wait_schema = props["wait_timeout_seconds"]
        self.assertNotIn("minimum", wait_schema)
        self.assertNotIn("maximum", wait_schema)
        description = wait_schema.get("description") or ""
        self.assertIn("clamped", description)

    def test_window_size_openapi_is_object(self):
        props = self.schema["components"]["schemas"]["ScrapeRequest"]["properties"]
        window_schema = props["window_size"]
        refs = _schema_ref_names(window_schema)
        self.assertIn("WindowSize", refs)
        size_schema = self.schema["components"]["schemas"]["WindowSize"]
        size_props = size_schema["properties"]
        self.assertIn("width", size_props)
        self.assertIn("height", size_props)
        self.assertNotIn("minItems", window_schema)
        self.assertNotIn("maxItems", window_schema)
        self.assertNotIn("scroll_to_bottom", props)

    def test_health_schema_includes_status_fields(self):
        health_200 = self.schema["paths"]["/health"]["get"]["responses"]["200"]
        refs = _schema_ref_names(health_200)
        self.assertIn("HealthResponse", refs)
        health_schema = self.schema["components"]["schemas"]["HealthResponse"]
        properties = health_schema["properties"]
        self.assertIn("status", properties)
        self.assertIn("service", properties)
        self.assertIn("botasaurus_version", properties)
        status_schema = properties["status"]
        self.assertTrue(
            status_schema.get("const") == "ok"
            or status_schema.get("enum") == ["ok"]
            or "ok" in (status_schema.get("examples") or [])
        )

    def test_xhr_responses_use_xhr_response_model(self):
        scrape_schema = self.schema["components"]["schemas"]["ScrapeSuccess"]
        refs = _schema_ref_names(scrape_schema["properties"]["xhr_responses"])
        self.assertIn("XhrResponse", refs)
        xhr_schema = self.schema["components"]["schemas"]["XhrResponse"]
        properties = xhr_schema["properties"]
        for field in ("url", "status_code", "headers", "body"):
            self.assertIn(field, properties)
        self.assertIn("diagnostics", scrape_schema["properties"])
        self.assertNotIn("ScrapeResponse", self.schema["components"]["schemas"])


if __name__ == "__main__":
    unittest.main()

import ipaddress
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from app.detector import ChallengeDetector
from app.engine import (
    DEFAULT_SCRAPE_TIMEOUT_SECONDS,
    ScrapeRequest,
    ScrapeResponse,
    ScraperEngine,
)
from app.metadata import MetadataExtractor
from app.security import UrlGuard


class _FakeMetadataResponse:
    status_code = 200
    headers = {"content-type": "text/html"}
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
        self.assertEqual(payload.execution_mode, "auto")
        self.assertEqual(payload.navigation_mode, "auto")
        self.assertEqual(payload.max_retries, 2)
        self.assertEqual(payload.wait_timeout_seconds, 15)
        self.assertFalse(payload.scroll)
        self.assertFalse(payload.scroll_to_bottom)
        self.assertFalse(payload.should_scroll)
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
        self.assertFalse(req_scroll.scroll_to_bottom)
        self.assertTrue(req_scroll.should_scroll)

        req_bottom = ScrapeRequest(url="https://example.com", scroll_to_bottom=True)
        self.assertFalse(req_bottom.scroll)
        self.assertTrue(req_bottom.scroll_to_bottom)
        self.assertTrue(req_bottom.should_scroll)

    def test_window_size_validation_requires_two_ints(self):
        with self.assertRaises(ValidationError):
            ScrapeRequest(url="https://example.com", window_size=[1920])

    def test_wait_timeout_seconds_clamps_gem_default_to_service_cap(self):
        with self.assertLogs("botasaurus_scrape_api", level="INFO") as captured:
            payload = ScrapeRequest(
                url="https://example.com", wait_timeout_seconds=28
            )

        self.assertEqual(payload.wait_timeout_seconds, DEFAULT_SCRAPE_TIMEOUT_SECONDS)
        self.assertEqual(DEFAULT_SCRAPE_TIMEOUT_SECONDS, 20)
        log_text = "\n".join(captured.output)
        self.assertIn("host=example.com", log_text)
        self.assertIn("field=wait_timeout_seconds", log_text)
        self.assertIn("from=28", log_text)
        self.assertIn("to=20", log_text)

    def test_wait_timeout_seconds_clamps_below_one(self):
        with self.assertLogs("botasaurus_scrape_api", level="INFO") as captured:
            payload = ScrapeRequest(
                url="https://example.com", wait_timeout_seconds=0
            )

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

        self.assertIsNone(result["error"])
        self.assertEqual(result["html"], "<html><body><h1>Example Domain</h1></body></html>")

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

        self.assertIsNone(result["error"])
        self.assertEqual(result["execution_tier"], "http_request")
        self.assertIsNotNone(result["headers"])
        self.assertEqual(
            result["headers"]["content-type"], "text/html; charset=utf-8"
        )
        self.assertNotIn("application/octet-stream", result["headers"].values())
        self.assertIn("Caffè", result["html"])
        self.assertNotIn("CaffÃ¨", result["html"])
        result["html"].encode("utf-8")

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

                self.assertIsNone(result["error"])
                self.assertEqual(result["execution_tier"], "browser_driver")
                self.assertIn("<article>", result["html"])
                self.assertIn("Headline", result["html"])
                self.assertEqual(
                    result["headers"]["content-type"], "text/html; charset=utf-8"
                )

    def test_strategy_selection(self):
        self.assertEqual(ScraperEngine.resolve_strategies("auto", 0), ["google_get"])
        self.assertEqual(
            ScraperEngine.resolve_strategies("auto", 2),
            ["google_get", "google_get_bypass", "get"],
        )
        self.assertEqual(ScraperEngine.resolve_strategies("get", 2), ["get", "get", "get"])
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

            self.assertEqual(result["error_category"], "navigation_error")
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
            window_size=[1920, 1080],
            lang="en-US",
            headless=True,
            proxy="http://proxy.example:8080",
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            engine = ScraperEngine(runtime_root=runtime_root)
            with patch("app.engine.Driver", _CaptureDriver):
                result = engine.execute(payload)

        self.assertIsNone(result["error"])
        self.assertIsNotNone(_CaptureDriver.last_init_kwargs)
        self.assertTrue(_CaptureDriver.last_init_kwargs["block_images"])
        self.assertTrue(_CaptureDriver.last_init_kwargs["block_images_and_css"])
        self.assertFalse(
            _CaptureDriver.last_init_kwargs["wait_for_complete_page_load"]
        )
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

        res = ChallengeDetector.detect("<html><body>Clean page</body></html>", 200, driver=mock_driver)
        self.assertTrue(res.challenge_detected)
        self.assertTrue(res.blocked_detected)
        self.assertEqual(res.detected_marker, "botasaurus_driver_bot_detected")
        self.assertEqual(res.error_category, "challenge_block")


class MetadataExtractorUnitTests(unittest.TestCase):
    def test_extract_passive_metadata_from_requests_list(self):
        class _Req:
            def __init__(self, status, headers, url):
                self.response = type("Resp", (), {"status_code": status, "headers": headers})()
                self.url = url

        driver = type("D", (), {"requests": [_Req(200, {"content-type": "text/html"}, "https://example.com/final")]})()
        status, headers, final_url = MetadataExtractor.extract_from_requests(driver, "https://example.com")
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
            {"get_log": lambda self, log_type: perf_log if log_type == "performance" else []},
        )()
        status, headers, final_url = MetadataExtractor.extract_from_cdp_logs(driver, "https://example.com")
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

    def test_scrape_response_factory_invariants(self):
        success = ScrapeResponse.create_success(
            "https://example.com",
            request_id="req-abc",
            html="<html></html>",
            attempts=1,
            strategy_used="get",
            render_ms=120,
            execution_tier="browser_driver",
        )
        self.assertEqual(success["status_code"], 200)
        self.assertIsNone(success["error"])
        self.assertEqual(success["execution_tier"], "browser_driver")
        self.assertEqual(success["final_url"], "https://example.com")
        self.assertEqual(success["xhr_responses"], [])
        self.assertEqual(
            success["headers"]["content-type"], "text/html; charset=utf-8"
        )

        with_xhr = ScrapeResponse.create_success(
            "https://example.com",
            request_id="req-xhr",
            html="<html></html>",
            attempts=1,
            strategy_used="get",
            render_ms=10,
            execution_tier="browser_driver",
            xhr_responses=[
                {
                    "url": "https://api.example.com/items",
                    "status_code": 200,
                    "headers": {"content-type": "application/json"},
                    "body": '{"items":[]}',
                }
            ],
        )
        self.assertEqual(len(with_xhr["xhr_responses"]), 1)
        self.assertEqual(
            with_xhr["xhr_responses"][0]["url"], "https://api.example.com/items"
        )

        err = ScrapeResponse.create_error(
            "https://example.com",
            "Something broke",
            request_id="req-err",
            error_category="navigation_error",
        )
        self.assertEqual(err["error"], "Something broke")
        self.assertEqual(err["error_category"], "navigation_error")
        self.assertEqual(err["html"], "")
        self.assertEqual(err["xhr_responses"], [])

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
        self.assertEqual(body["error_category"], "navigation_error")
        self.assertTrue(body["request_id"])
        log_text = "\n".join(captured.output)
        self.assertIn("request_schema_422", log_text)
        self.assertIn("host=example.com", log_text)
        self.assertIn("field=window_size", log_text)


if __name__ == "__main__":
    unittest.main()

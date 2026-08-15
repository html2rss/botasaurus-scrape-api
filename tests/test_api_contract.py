import ipaddress
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app import main
from app.detector import ChallengeDetector
from app.engine import ScraperEngine
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

    def get(self, *_args, **_kwargs):
        return None

    def google_get(self, *_args, **_kwargs):
        return None

    def wait_for_element(self, *_args, **_kwargs):
        raise RuntimeError("missing selector")

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


class MainUnitTests(unittest.TestCase):
    def test_request_defaults(self):
        payload = main.ScrapeRequest(url="https://example.com")
        self.assertEqual(payload.execution_mode, "auto")
        self.assertEqual(payload.navigation_mode, "auto")
        self.assertEqual(payload.max_retries, 2)
        self.assertEqual(payload.wait_timeout_seconds, 15)
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

    def test_window_size_validation_requires_two_ints(self):
        with self.assertRaises(ValidationError):
            main.ScrapeRequest(url="https://example.com", window_size=[1920])

    def test_strategy_selection(self):
        self.assertEqual(main._strategies_for_request("auto", 0), ["google_get"])
        self.assertEqual(
            main._strategies_for_request("auto", 2),
            ["google_get", "google_get_bypass", "get"],
        )
        self.assertEqual(main._strategies_for_request("get", 2), ["get", "get", "get"])

    def test_challenge_detection_marker(self):
        blocked, challenge, marker = main._detect_block_challenge(
            '<span id="challenge-error-text">Enable JavaScript and cookies to continue</span>',
            200,
        )
        self.assertTrue(blocked)
        self.assertTrue(challenge)
        self.assertEqual(marker, "challenge-error-text")

    def test_cleanup_runs_on_navigation_error(self):
        payload = main.ScrapeRequest(
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
            with patch.object(main, "_engine", engine), patch("app.engine.Driver", _FakeDriver):
                result = main._run_scrape(payload)

            self.assertEqual(result["error_category"], "navigation_error")
            self.assertEqual(list(runtime_root.iterdir()), [])

    def test_run_scrape_forwards_driver_kwargs(self):
        _CaptureDriver.last_init_kwargs = None
        payload = main.ScrapeRequest(
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
            with patch.object(main, "_engine", engine), patch("app.engine.Driver", _CaptureDriver):
                result = main._run_scrape(payload)

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

    def test_extract_passive_metadata_from_requests_list(self):
        class _Req:
            def __init__(self, status, headers, url):
                self.response = type("Resp", (), {"status_code": status, "headers": headers})()
                self.url = url

        driver = type("D", (), {"requests": [_Req(200, {"content-type": "text/html"}, "https://example.com/final")]})()
        status, headers, final_url = main._extract_passive_metadata(driver, "https://example.com")
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
        status, headers, final_url = main._extract_passive_metadata(driver, "https://example.com")
        self.assertEqual(status, 200)
        self.assertEqual(headers, {"content-type": "text/html"})
        self.assertEqual(final_url, "https://example.com/cdp-final")

    def test_is_blocked_ip_allows_well_known_nat64_prefix(self):
        nat64_ip = ipaddress.ip_address("64:ff9b::3691:8e03")
        self.assertFalse(main._is_blocked_ip(nat64_ip))

    def test_is_blocked_ip_still_blocks_loopback(self):
        loopback = ipaddress.ip_address("127.0.0.1")
        self.assertTrue(main._is_blocked_ip(loopback))


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


class MetadataExtractorUnitTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

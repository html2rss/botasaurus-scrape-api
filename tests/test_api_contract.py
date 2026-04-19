import ipaddress
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app import main


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
        self.assertEqual(payload.navigation_mode, "auto")
        self.assertEqual(payload.max_retries, 2)
        self.assertFalse(payload.block_images)
        self.assertFalse(payload.block_images_and_css)
        self.assertTrue(payload.wait_for_complete_page_load)
        self.assertIsNone(payload.user_agent)
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
        blocked, challenge = main._detect_block_challenge(
            '<span id="challenge-error-text">Enable JavaScript and cookies to continue</span>',
            200,
        )
        self.assertTrue(blocked)
        self.assertTrue(challenge)

    def test_cleanup_runs_on_navigation_error(self):
        payload = main.ScrapeRequest(
            url="https://example.com",
            navigation_mode="get",
            max_retries=0,
            wait_for_selector="#missing",
            wait_timeout_seconds=1,
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            with patch.object(main, "_RUNTIME_ROOT", runtime_root), patch.object(main, "Driver", _FakeDriver):
                result = main._run_scrape(payload)

            self.assertEqual(result["error_category"], "navigation_error")
            self.assertEqual(list(runtime_root.iterdir()), [])

    def test_run_scrape_forwards_driver_kwargs(self):
        _CaptureDriver.last_init_kwargs = None
        payload = main.ScrapeRequest(
            url="https://example.com",
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
            with patch.object(main, "_RUNTIME_ROOT", runtime_root), patch.object(
                main, "Driver", _CaptureDriver
            ):
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

    def test_is_blocked_ip_allows_well_known_nat64_prefix(self):
        nat64_ip = ipaddress.ip_address("64:ff9b::3691:8e03")
        self.assertFalse(main._is_blocked_ip(nat64_ip))

    def test_is_blocked_ip_still_blocks_loopback(self):
        loopback = ipaddress.ip_address("127.0.0.1")
        self.assertTrue(main._is_blocked_ip(loopback))


if __name__ == "__main__":
    unittest.main()

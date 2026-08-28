"""Browser-tier challenge-before-timeout and hard-block abort."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

from app.config import get_settings
from app.engine import ScraperEngine
from app.schemas.enums import ErrorCategory, ExecutionMode, NavigationMode, TimeoutPhase
from app.schemas.response import ScrapeError
from tests.support.factories import scrape_request
from tests.support.fakes import FakeDriver


class _PassiveRequest:
    def __init__(self, status: int, url: str) -> None:
        self.url = url
        self.response = type(
            "Resp",
            (),
            {"status_code": status, "headers": {"content-type": "text/html"}},
        )()


class _ChallengeHtmlDriver(FakeDriver):
    """Timeout on navigate while page already shows a challenge interstitial."""

    navigate_calls: ClassVar[int] = 0

    def __init__(self, *args: object, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.page_html = "<html>Just a moment...</html>"
        self.current_url = "https://example.com/"

    @classmethod
    def reset(cls) -> None:
        cls.navigate_calls = 0

    def get(self, *_args: object, **_kwargs: Any) -> None:
        type(self).navigate_calls += 1
        raise TimeoutError("Navigation timeout after 15s")

    def google_get(self, *_args: object, **_kwargs: Any) -> None:
        type(self).navigate_calls += 1
        raise TimeoutError("Navigation timeout after 15s")


class _CleanTimeoutDriver(FakeDriver):
    """Timeout on navigate with a clean page body."""

    def __init__(self, *args: object, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.page_html = "<html><body><h1>Example Domain</h1></body></html>"
        self.current_url = "https://example.com/"

    def get(self, *_args: object, **_kwargs: Any) -> None:
        raise TimeoutError("Navigation timeout after 15s")

    def google_get(self, *_args: object, **_kwargs: Any) -> None:
        raise TimeoutError("Navigation timeout after 15s")


class _HardBlockDriver(FakeDriver):
    """HTTP hard block without challenge marker — must not burn more strategies."""

    navigate_calls: ClassVar[int] = 0

    def __init__(self, *args: object, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.page_html = "<html><h1>Forbidden</h1></body></html>"
        self.current_url = "https://example.com/"
        self.requests = [_PassiveRequest(403, "https://example.com/")]

    @classmethod
    def reset(cls) -> None:
        cls.navigate_calls = 0

    def get(self, *_args: object, **_kwargs: Any) -> None:
        type(self).navigate_calls += 1

    def google_get(self, *_args: object, **_kwargs: Any) -> None:
        type(self).navigate_calls += 1


class _SoftChallengeDriver(FakeDriver):
    """Soft challenge marker — may retry remaining strategies."""

    navigate_calls: ClassVar[int] = 0

    def __init__(self, *args: object, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.page_html = "<html>Just a moment...</html>"
        self.current_url = "https://example.com/"

    @classmethod
    def reset(cls) -> None:
        cls.navigate_calls = 0

    def get(self, *_args: object, **_kwargs: Any) -> None:
        type(self).navigate_calls += 1

    def google_get(self, *_args: object, **_kwargs: Any) -> None:
        type(self).navigate_calls += 1


class BrowserTierChallengeTests(unittest.TestCase):
    def test_timeout_with_challenge_html_returns_challenge_block(self) -> None:
        _ChallengeHtmlDriver.reset()
        payload = scrape_request(
            execution_mode=ExecutionMode.BROWSER,
            navigation_mode=NavigationMode.GET,
            max_retries=0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with patch("botasaurus.browser.Driver", _ChallengeHtmlDriver):
                result = engine.execute(payload, request_id="req-timeout-challenge")

        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        self.assertEqual(result.error_category, ErrorCategory.CHALLENGE_BLOCK)
        self.assertIsNone(result.diagnostics.timeout_phase)
        self.assertIsNotNone(result.diagnostics.challenge)
        assert result.diagnostics.challenge is not None
        self.assertTrue(result.diagnostics.challenge.detected)
        self.assertEqual(_ChallengeHtmlDriver.navigate_calls, 1)

    def test_timeout_with_clean_page_returns_timeout_work(self) -> None:
        payload = scrape_request(
            execution_mode=ExecutionMode.BROWSER,
            navigation_mode=NavigationMode.GET,
            max_retries=0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with patch("botasaurus.browser.Driver", _CleanTimeoutDriver):
                result = engine.execute(payload, request_id="req-timeout-clean")

        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        self.assertEqual(result.error_category, ErrorCategory.TIMEOUT)
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.WORK)

    def test_hard_block_aborts_remaining_strategies(self) -> None:
        _HardBlockDriver.reset()
        payload = scrape_request(
            execution_mode=ExecutionMode.BROWSER,
            navigation_mode=NavigationMode.AUTO,
            max_retries=2,
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with patch("botasaurus.browser.Driver", _HardBlockDriver):
                result = engine.execute(payload, request_id="req-hard-block")

        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        self.assertEqual(result.error_category, ErrorCategory.CHALLENGE_BLOCK)
        self.assertIsNone(result.diagnostics.timeout_phase)
        self.assertEqual(result.diagnostics.attempts, 1)
        self.assertEqual(_HardBlockDriver.navigate_calls, 1)
        assert result.diagnostics.challenge is not None
        self.assertTrue(result.diagnostics.challenge.blocked)
        self.assertFalse(result.diagnostics.challenge.detected)

    def test_soft_challenge_retries_strategies_then_blocks(self) -> None:
        _SoftChallengeDriver.reset()
        payload = scrape_request(
            execution_mode=ExecutionMode.BROWSER,
            navigation_mode=NavigationMode.AUTO,
            max_retries=2,
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with patch("botasaurus.browser.Driver", _SoftChallengeDriver):
                result = engine.execute(payload, request_id="req-soft-challenge")

        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        self.assertEqual(result.error_category, ErrorCategory.CHALLENGE_BLOCK)
        self.assertEqual(result.diagnostics.attempts, 3)
        self.assertEqual(_SoftChallengeDriver.navigate_calls, 3)
        assert result.diagnostics.challenge is not None
        self.assertTrue(result.diagnostics.challenge.detected)

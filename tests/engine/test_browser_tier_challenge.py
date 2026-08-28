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


class _ScenarioDriver(FakeDriver):
    """Shared navigate counter + optional timeout / HTTP status fixtures."""

    navigate_calls: ClassVar[int] = 0
    page_body: ClassVar[str] = "<html><body>ok</body></html>"
    timeout_on_nav: ClassVar[bool] = False
    http_status: ClassVar[int | None] = None

    def __init__(self, *args: object, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.page_html = type(self).page_body
        self.current_url = "https://example.com/"
        status = type(self).http_status
        if status is not None:
            self.requests = [_PassiveRequest(status, "https://example.com/")]

    @classmethod
    def reset(cls) -> None:
        cls.navigate_calls = 0

    def _navigate(self) -> None:
        type(self).navigate_calls += 1
        if type(self).timeout_on_nav:
            raise TimeoutError("Navigation timeout after 15s")

    def get(self, *_args: object, **_kwargs: Any) -> None:
        self._navigate()

    def google_get(self, *_args: object, **_kwargs: Any) -> None:
        self._navigate()


class _ChallengeHtmlDriver(_ScenarioDriver):
    navigate_calls: ClassVar[int] = 0
    page_body = "<html>Just a moment...</html>"
    timeout_on_nav = True


class _CleanTimeoutDriver(_ScenarioDriver):
    page_body = "<html><body><h1>Example Domain</h1></body></html>"
    timeout_on_nav = True


class _HardBlockDriver(_ScenarioDriver):
    navigate_calls: ClassVar[int] = 0
    page_body = "<html><h1>Forbidden</h1></body></html>"
    http_status = 403


class _SoftChallengeDriver(_ScenarioDriver):
    navigate_calls: ClassVar[int] = 0
    page_body = "<html>Just a moment...</html>"


class BrowserTierChallengeTests(unittest.TestCase):
    def _execute(
        self,
        driver_cls: type[_ScenarioDriver],
        *,
        navigation_mode: NavigationMode,
        max_retries: int,
        request_id: str,
    ) -> ScrapeError:
        driver_cls.reset()
        payload = scrape_request(
            execution_mode=ExecutionMode.BROWSER,
            navigation_mode=navigation_mode,
            max_retries=max_retries,
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with patch("botasaurus.browser.Driver", driver_cls):
                result = engine.execute(payload, request_id=request_id)
        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        return result

    def test_timeout_with_challenge_html_returns_challenge_block(self) -> None:
        result = self._execute(
            _ChallengeHtmlDriver,
            navigation_mode=NavigationMode.GET,
            max_retries=0,
            request_id="req-timeout-challenge",
        )
        self.assertEqual(result.error_category, ErrorCategory.CHALLENGE_BLOCK)
        self.assertIsNone(result.diagnostics.timeout_phase)
        assert result.diagnostics.challenge is not None
        self.assertTrue(result.diagnostics.challenge.detected)
        self.assertEqual(_ChallengeHtmlDriver.navigate_calls, 1)

    def test_timeout_with_clean_page_returns_timeout_work(self) -> None:
        result = self._execute(
            _CleanTimeoutDriver,
            navigation_mode=NavigationMode.GET,
            max_retries=0,
            request_id="req-timeout-clean",
        )
        self.assertEqual(result.error_category, ErrorCategory.TIMEOUT)
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.WORK)

    def test_hard_block_aborts_remaining_strategies(self) -> None:
        result = self._execute(
            _HardBlockDriver,
            navigation_mode=NavigationMode.AUTO,
            max_retries=2,
            request_id="req-hard-block",
        )
        self.assertEqual(result.error_category, ErrorCategory.CHALLENGE_BLOCK)
        self.assertIsNone(result.diagnostics.timeout_phase)
        self.assertEqual(result.diagnostics.attempts, 1)
        self.assertEqual(_HardBlockDriver.navigate_calls, 1)
        assert result.diagnostics.challenge is not None
        self.assertTrue(result.diagnostics.challenge.blocked)
        self.assertFalse(result.diagnostics.challenge.detected)

    def test_soft_challenge_retries_strategies_then_blocks(self) -> None:
        result = self._execute(
            _SoftChallengeDriver,
            navigation_mode=NavigationMode.AUTO,
            max_retries=2,
            request_id="req-soft-challenge",
        )
        self.assertEqual(result.error_category, ErrorCategory.CHALLENGE_BLOCK)
        self.assertEqual(result.diagnostics.attempts, 3)
        self.assertEqual(_SoftChallengeDriver.navigate_calls, 3)
        assert result.diagnostics.challenge is not None
        self.assertTrue(result.diagnostics.challenge.detected)

    def test_soft_challenge_does_not_retry_when_work_budget_low(self) -> None:
        """Below soft-retry floor, unclean assessment is challenge_block immediately."""
        _SoftChallengeDriver.reset()
        payload = scrape_request(
            execution_mode=ExecutionMode.BROWSER,
            navigation_mode=NavigationMode.AUTO,
            max_retries=2,
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with (
                patch("botasaurus.browser.Driver", _SoftChallengeDriver),
                patch(
                    "app.engine.browser_tier.remaining_work_seconds",
                    return_value=4,
                ),
            ):
                result = engine.execute(payload, request_id="req-soft-low-budget")
        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        self.assertEqual(result.error_category, ErrorCategory.CHALLENGE_BLOCK)
        self.assertEqual(result.diagnostics.attempts, 1)
        self.assertEqual(_SoftChallengeDriver.navigate_calls, 1)

    def test_mid_wait_challenge_returns_challenge_block(self) -> None:
        class _MidWaitChallengeDriver(_ScenarioDriver):
            navigate_calls: ClassVar[int] = 0
            page_body = "<html>Just a moment...</html>"
            wait_calls: ClassVar[int] = 0

            def wait_for_element(self, *_args: object, **_kwargs: Any) -> None:
                type(self).wait_calls += 1
                raise TimeoutError("element not found")

        _MidWaitChallengeDriver.reset()
        _MidWaitChallengeDriver.wait_calls = 0
        payload = scrape_request(
            execution_mode=ExecutionMode.BROWSER,
            navigation_mode=NavigationMode.GET,
            max_retries=0,
            wait_for_selector="#content",
            wait_timeout_seconds=4,
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            with patch("botasaurus.browser.Driver", _MidWaitChallengeDriver):
                result = engine.execute(payload, request_id="req-mid-wait")
        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        self.assertEqual(result.error_category, ErrorCategory.CHALLENGE_BLOCK)
        self.assertIsNone(result.diagnostics.timeout_phase)
        # First chunk probes challenge and fails closed — no full 4s burn.
        self.assertEqual(_MidWaitChallengeDriver.wait_calls, 1)
        self.assertEqual(_MidWaitChallengeDriver.navigate_calls, 1)

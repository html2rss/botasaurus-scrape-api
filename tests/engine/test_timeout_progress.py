# pyright: reportMissingParameterType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false, reportPrivateUsage=false, reportAttributeAccessIssue=false, reportFunctionMemberAccess=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportOptionalSubscript=false, reportOptionalMemberAccess=false
"""Engine progress marking across queue, boot, and work phases."""

from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import get_settings
from app.engine import ScraperEngine
from app.infra.scrape_progress import ScrapeProgress
from app.schemas.enums import (
    ExecutionMode,
    ExecutionTier,
    NavigationMode,
    TimeoutPhase,
)
from tests.support.factories import scrape_request
from tests.support.fakes import fake_request_cls

_URL = "https://example.com"
_HTML = "<html><body><h1>Example Domain</h1></body></html>"


class _PhaseProbeDriver:
    """Records progress phase at Driver construction time."""

    construction_phase: TimeoutPhase | None = None
    progress: ScrapeProgress | None = None

    def __init__(self, *args, **kwargs):
        del args, kwargs
        progress = type(self).progress
        type(self).construction_phase = (
            progress.snapshot().phase if progress is not None else None
        )
        self.page_html = _HTML
        self.current_url = f"{_URL}/"
        self.requests = SimpleNamespace(
            get=lambda _url: SimpleNamespace(
                status_code=200,
                headers={"content-type": "text/html"},
                url=self.current_url,
            )
        )

    def __getattr__(self, _name):
        return lambda *_a, **_k: None


def _execute(payload, *, progress, request_id, **patches):
    with tempfile.TemporaryDirectory() as tmp:
        engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
        with ExitStack() as stack:
            if "Driver" in patches:
                stack.enter_context(
                    patch("botasaurus.browser.Driver", patches["Driver"])
                )
            if "Request" in patches:
                stack.enter_context(
                    patch("botasaurus.request.Request", patches["Request"])
                )
            return engine.execute(payload, request_id=request_id, progress=progress)


def _snap_eq(test, snap, *, phase, attempts=0, strategy=None, tier=None):
    test.assertEqual(snap.phase, phase)
    test.assertEqual(snap.attempts, attempts)
    test.assertEqual(snap.strategy_used, strategy)
    test.assertEqual(snap.execution_tier, tier)


class EngineProgressMarkTests(unittest.TestCase):
    def test_execute_queue_timeout_sets_phase(self):
        progress = ScrapeProgress()
        result = ScraperEngine(settings=get_settings()).execute(
            scrape_request(
                execution_mode=ExecutionMode.BROWSER,
                navigation_mode=NavigationMode.GET,
            ),
            time.monotonic() - 1,
            request_id="req-engine-queue",
            progress=progress,
        )
        self.assertEqual(result.error_category.value, "timeout")
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.QUEUE)
        self.assertEqual(progress.snapshot().phase, TimeoutPhase.QUEUE)

    def test_browser_tier_marks_boot_before_driver_then_work(self):
        progress = ScrapeProgress()
        _PhaseProbeDriver.progress = progress
        _PhaseProbeDriver.construction_phase = None
        result = _execute(
            scrape_request(
                execution_mode=ExecutionMode.BROWSER,
                navigation_mode=NavigationMode.GET,
                max_retries=0,
            ),
            progress=progress,
            request_id="req-boot-mark",
            Driver=_PhaseProbeDriver,
        )
        self.assertIsNone(getattr(result, "error", None))
        self.assertEqual(_PhaseProbeDriver.construction_phase, TimeoutPhase.BOOT)
        _snap_eq(
            self,
            progress.snapshot(),
            phase=TimeoutPhase.WORK,
            attempts=1,
            strategy=NavigationMode.GET,
            tier=ExecutionTier.BROWSER_DRIVER,
        )

    def test_request_tier_marks_work_with_attempt(self):
        progress = ScrapeProgress()
        result = _execute(
            scrape_request(url=_URL, execution_mode=ExecutionMode.REQUEST),
            progress=progress,
            request_id="req-http-mark",
            Request=fake_request_cls(html=_HTML, url=f"{_URL}/"),
        )
        self.assertIsNone(getattr(result, "error", None))
        _snap_eq(
            self,
            progress.snapshot(),
            phase=TimeoutPhase.WORK,
            attempts=1,
            tier=ExecutionTier.HTTP_REQUEST,
        )

    def test_request_tier_timeout_exception_sets_phase(self):
        class BoomRequest:
            def get(self, *_a, **_k):
                raise TimeoutError("HTTP read timeout")

            def close(self):
                return None

        progress = ScrapeProgress()
        result = _execute(
            scrape_request(url=_URL, execution_mode=ExecutionMode.REQUEST),
            progress=progress,
            request_id="req-http-timeout",
            Request=BoomRequest,
        )
        self.assertEqual(result.error_category.value, "timeout")
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.WORK)
        self.assertEqual(result.diagnostics.execution_tier, ExecutionTier.HTTP_REQUEST)
        self.assertEqual(progress.snapshot().phase, TimeoutPhase.WORK)

    def test_browser_tier_timeout_exception_sets_phase(self):
        class BoomDriver(_PhaseProbeDriver):
            def get(self, *_a, **_k):
                raise TimeoutError("navigation timeout")

        progress = ScrapeProgress()
        BoomDriver.progress = progress
        result = _execute(
            scrape_request(
                execution_mode=ExecutionMode.BROWSER,
                navigation_mode=NavigationMode.GET,
                max_retries=0,
            ),
            progress=progress,
            request_id="req-browser-timeout",
            Driver=BoomDriver,
        )
        self.assertEqual(result.error_category.value, "timeout")
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.WORK)
        self.assertEqual(result.diagnostics.strategy_used, NavigationMode.GET)


if __name__ == "__main__":
    unittest.main()

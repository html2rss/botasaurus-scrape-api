# tests/test_timeout_phase.py
from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import get_settings
from app.domain.scrape_service import ScrapeService
from app.engine import ScraperEngine
from app.infra.scrape_progress import ScrapeProgress
from app.schemas import (
    ExecutionMode,
    ExecutionTier,
    NavigationMode,
    ScrapeRequest,
    TimeoutPhase,
)

_URL = "https://example.com"
_HTML = "<html><body><h1>Example Domain</h1></body></html>"


def _snap_eq(test, snap, *, phase, attempts=0, strategy=None, tier=None):
    test.assertEqual(snap.phase, phase)
    test.assertEqual(snap.attempts, attempts)
    test.assertEqual(snap.strategy_used, strategy)
    test.assertEqual(snap.execution_tier, tier)


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


def _fake_request_cls(html=_HTML, url=f"{_URL}/"):
    response = SimpleNamespace(
        text=html,
        status_code=200,
        headers={"content-type": "text/html"},
        url=url,
    )
    return type(
        "FakeRequest",
        (),
        {"get": lambda self, *_a, **_k: response, "close": lambda self: None},
    )


def _execute(payload, *, progress, request_id, **patches):
    with tempfile.TemporaryDirectory() as tmp:
        engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
        with ExitStack() as stack:
            if "Driver" in patches:
                stack.enter_context(
                    patch("app.engine.browser_tier.Driver", patches["Driver"])
                )
            if "Request" in patches:
                stack.enter_context(
                    patch("app.engine.request_tier.Request", patches["Request"])
                )
            return engine.execute(payload, request_id=request_id, progress=progress)


class ScrapeProgressTests(unittest.TestCase):
    def test_snapshot_defaults_to_queue(self):
        _snap_eq(self, ScrapeProgress().snapshot(), phase=TimeoutPhase.QUEUE)

    def test_mark_updates_snapshot(self):
        progress = ScrapeProgress()
        progress.mark(
            TimeoutPhase.WORK,
            attempts=2,
            strategy_used=NavigationMode.GET,
            execution_tier=ExecutionTier.BROWSER_DRIVER,
        )
        _snap_eq(
            self,
            progress.snapshot(),
            phase=TimeoutPhase.WORK,
            attempts=2,
            strategy=NavigationMode.GET,
            tier=ExecutionTier.BROWSER_DRIVER,
        )


class HandlerTimeoutErrorTests(unittest.TestCase):
    def test_queue_phase_keeps_zero_attempts(self):
        result = ScrapeService.build_timeout_error(
            _URL,
            request_id="req-queue",
            started_monotonic=time.monotonic(),
            progress=ScrapeProgress(),
            timeout_seconds=45,
        )
        self.assertEqual(result.error_category.value, "timeout")
        self.assertIn("phase=queue", result.error)
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.QUEUE)
        self.assertEqual(result.diagnostics.attempts, 0)
        self.assertIsNone(result.diagnostics.strategy_used)

    def test_work_phase_preserves_attempts_and_strategy(self):
        progress = ScrapeProgress()
        progress.mark(
            TimeoutPhase.WORK,
            attempts=2,
            strategy_used=NavigationMode.GOOGLE_GET,
            execution_tier=ExecutionTier.BROWSER_DRIVER,
        )
        result = ScrapeService.build_timeout_error(
            _URL,
            request_id="req-work",
            started_monotonic=time.monotonic() - 1,
            progress=progress,
            timeout_seconds=45,
        )
        d = result.diagnostics
        self.assertEqual(d.timeout_phase, TimeoutPhase.WORK)
        self.assertEqual(d.attempts, 2)
        self.assertEqual(d.strategy_used, NavigationMode.GOOGLE_GET)
        self.assertEqual(d.execution_tier, ExecutionTier.BROWSER_DRIVER)
        self.assertIn("phase=work", result.error)
        self.assertGreaterEqual(d.render_ms, 0)


class EngineProgressMarkTests(unittest.TestCase):
    def test_execute_queue_timeout_sets_phase(self):
        progress = ScrapeProgress()
        result = ScraperEngine(settings=get_settings()).execute(
            ScrapeRequest(
                url=_URL,
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
            ScrapeRequest(
                url=_URL,
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
            ScrapeRequest(url=_URL, execution_mode=ExecutionMode.REQUEST),
            progress=progress,
            request_id="req-http-mark",
            Request=_fake_request_cls(),
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
            ScrapeRequest(url=_URL, execution_mode=ExecutionMode.REQUEST),
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
            ScrapeRequest(
                url=_URL,
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


class HandlerTimeoutHttpTests(unittest.TestCase):
    def test_scrape_handler_timeout_uses_progress(self):
        from tests.support.http import test_client

        def fake_execute(_payload, _deadline=None, *, request_id=None, progress=None):
            assert progress is not None
            progress.mark(
                TimeoutPhase.BOOT, execution_tier=ExecutionTier.BROWSER_DRIVER
            )

        async def boom(awaitable, timeout=None):
            del timeout
            await awaitable
            raise TimeoutError

        with (
            test_client(execute_side_effect=fake_execute) as client,
            patch("asyncio.wait_for", side_effect=boom),
        ):
            response = client.post(
                "/scrape",
                json={
                    "url": _URL,
                    "execution_mode": "browser",
                    "navigation_mode": "get",
                },
            )

        self.assertEqual(response.status_code, 504)
        body = response.json()
        self.assertEqual(body["error_category"], "timeout")
        self.assertEqual(body["diagnostics"]["timeout_phase"], "boot")
        self.assertEqual(body["diagnostics"]["attempts"], 0)
        self.assertEqual(body["diagnostics"]["execution_tier"], "browser_driver")
        self.assertIn("phase=boot", body["error"])


if __name__ == "__main__":
    unittest.main()

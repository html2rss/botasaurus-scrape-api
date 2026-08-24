# tests/test_timeout_phase.py
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from app.engine import ScraperEngine
from app.main import handler_timeout_error
from app.schemas import (
    ExecutionMode,
    ExecutionTier,
    NavigationMode,
    ScrapeRequest,
    TimeoutPhase,
)
from app.scrape_progress import ScrapeProgress


class _FakeMetadataResponse:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "text/html"}
    url = "https://example.com/"


class _FakeRequests:
    def get(self, _url):
        return _FakeMetadataResponse()


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
        self.page_html = "<html><body><h1>Example Domain</h1></body></html>"
        self.current_url = "https://example.com/"
        self.requests = _FakeRequests()

    def get(self, *_args, **_kwargs):
        return None

    def google_get(self, *_args, **_kwargs):
        return None

    def organic_get(self, *_args, **_kwargs):
        return None

    def wait_for_element(self, *_args, **_kwargs):
        return None

    def scroll_to_bottom(self):
        return None

    def sleep(self, *_args, **_kwargs):
        return None

    def close(self):
        return None


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


class ScrapeProgressTests(unittest.TestCase):
    def test_snapshot_defaults_to_queue(self):
        progress = ScrapeProgress()
        snap = progress.snapshot()
        self.assertEqual(snap.phase, TimeoutPhase.QUEUE)
        self.assertEqual(snap.attempts, 0)
        self.assertIsNone(snap.strategy_used)
        self.assertIsNone(snap.execution_tier)

    def test_mark_updates_snapshot(self):
        progress = ScrapeProgress()
        progress.mark(
            TimeoutPhase.WORK,
            attempts=2,
            strategy_used=NavigationMode.GET,
            execution_tier=ExecutionTier.BROWSER_DRIVER,
        )
        snap = progress.snapshot()
        self.assertEqual(snap.phase, TimeoutPhase.WORK)
        self.assertEqual(snap.attempts, 2)
        self.assertEqual(snap.strategy_used, NavigationMode.GET)
        self.assertEqual(snap.execution_tier, ExecutionTier.BROWSER_DRIVER)


class HandlerTimeoutErrorTests(unittest.TestCase):
    def test_queue_phase_keeps_zero_attempts(self):
        progress = ScrapeProgress()
        started = time.monotonic()
        result = handler_timeout_error(
            "https://example.com",
            request_id="req-queue",
            started_monotonic=started,
            progress=progress,
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
        result = handler_timeout_error(
            "https://example.com",
            request_id="req-work",
            started_monotonic=time.monotonic() - 1,
            progress=progress,
            timeout_seconds=45,
        )
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.WORK)
        self.assertEqual(result.diagnostics.attempts, 2)
        self.assertEqual(result.diagnostics.strategy_used, NavigationMode.GOOGLE_GET)
        self.assertEqual(
            result.diagnostics.execution_tier, ExecutionTier.BROWSER_DRIVER
        )
        self.assertIn("phase=work", result.error)
        self.assertGreaterEqual(result.diagnostics.render_ms, 0)


class EngineProgressMarkTests(unittest.TestCase):
    def test_execute_queue_timeout_sets_phase(self):
        engine = ScraperEngine()
        progress = ScrapeProgress()
        payload = ScrapeRequest(
            url="https://example.com",
            execution_mode=ExecutionMode.BROWSER,
            navigation_mode=NavigationMode.GET,
        )
        result = engine.execute(
            payload,
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
        payload = ScrapeRequest(
            url="https://example.com",
            execution_mode=ExecutionMode.BROWSER,
            navigation_mode=NavigationMode.GET,
            max_retries=0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(runtime_root=Path(tmp))
            with patch("app.engine.Driver", _PhaseProbeDriver):
                result = engine.execute(
                    payload, request_id="req-boot-mark", progress=progress
                )

        self.assertIsNone(getattr(result, "error", None))
        self.assertEqual(_PhaseProbeDriver.construction_phase, TimeoutPhase.BOOT)
        snap = progress.snapshot()
        self.assertEqual(snap.phase, TimeoutPhase.WORK)
        self.assertEqual(snap.attempts, 1)
        self.assertEqual(snap.strategy_used, NavigationMode.GET)
        self.assertEqual(snap.execution_tier, ExecutionTier.BROWSER_DRIVER)

    def test_request_tier_marks_work_with_attempt(self):
        progress = ScrapeProgress()
        _FakeRequest.response = _FakeHttpResponse(
            text="<html><body><h1>Example Domain</h1></body></html>",
            status_code=200,
            headers={"content-type": "text/html"},
            url="https://example.com/",
        )
        payload = ScrapeRequest(
            url="https://example.com",
            execution_mode=ExecutionMode.REQUEST,
        )

        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(runtime_root=Path(tmp))
            with patch("app.engine.Request", _FakeRequest):
                result = engine.execute(
                    payload, request_id="req-http-mark", progress=progress
                )

        self.assertIsNone(getattr(result, "error", None))
        snap = progress.snapshot()
        self.assertEqual(snap.phase, TimeoutPhase.WORK)
        self.assertEqual(snap.attempts, 1)
        self.assertEqual(snap.execution_tier, ExecutionTier.HTTP_REQUEST)


class HandlerTimeoutHttpTests(unittest.TestCase):
    def test_scrape_handler_timeout_uses_progress(self):
        from fastapi.testclient import TestClient

        import app.main as main_mod
        from app.main import app

        def fake_execute(payload, _deadline=None, *, request_id=None, progress=None):
            assert progress is not None
            progress.mark(
                TimeoutPhase.BOOT,
                execution_tier=ExecutionTier.BROWSER_DRIVER,
            )
            return None

        async def boom(_awaitable, timeout=None):
            del timeout
            # Drain the executor future so the fake_execute side effect runs.
            await _awaitable
            raise TimeoutError

        with (
            patch.object(main_mod._engine, "execute", side_effect=fake_execute),
            patch("asyncio.wait_for", side_effect=boom),
        ):
            client = TestClient(app)
            response = client.post(
                "/scrape",
                json={
                    "url": "https://example.com",
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

"""Engine lease phase marking across queue, boot, and work phases."""

from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.config import get_settings
from app.engine import ScraperEngine
from app.engine.work_lease import LeaseSnapshot, WorkLease
from app.schemas.enums import (
    ExecutionMode,
    ExecutionTier,
    NavigationMode,
    TimeoutPhase,
)
from app.schemas.request import ScrapeRequest
from app.schemas.response import ScrapeError, ScrapeSuccess
from tests.support.factories import scrape_request
from tests.support.fakes import FakeDriver, fake_request_cls

_URL = "https://example.com"
_HTML = "<html><body><h1>Example Domain</h1></body></html>"


class _PhaseProbeDriver(FakeDriver):
    """Records lease phase at Driver construction time."""

    construction_phase: TimeoutPhase | None = None
    lease: WorkLease | None = None

    def __init__(self, *args: object, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        lease = type(self).lease
        type(self).construction_phase = (
            lease.snapshot().phase if lease is not None else None
        )
        self.page_html = _HTML
        self.current_url = f"{_URL}/"


def _execute(
    payload: ScrapeRequest,
    *,
    lease: WorkLease,
    request_id: str,
    **patches: Any,
) -> ScrapeSuccess | ScrapeError:
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
            return engine.execute(payload, request_id=request_id, lease=lease)


def _snap_eq(
    test: unittest.TestCase,
    snap: LeaseSnapshot,
    *,
    phase: TimeoutPhase,
    attempts: int = 0,
    strategy: NavigationMode | None = None,
    tier: ExecutionTier | None = None,
) -> None:
    test.assertEqual(snap.phase, phase)
    test.assertEqual(snap.attempts, attempts)
    test.assertEqual(snap.strategy_used, strategy)
    test.assertEqual(snap.execution_tier, tier)


class EngineLeaseMarkTests(unittest.TestCase):
    def test_execute_queue_timeout_sets_phase(self) -> None:
        lease = WorkLease.tracking_only()
        result = ScraperEngine(settings=get_settings()).execute(
            scrape_request(
                execution_mode=ExecutionMode.BROWSER,
                navigation_mode=NavigationMode.GET,
            ),
            time.monotonic() - 1,
            request_id="req-engine-queue",
            lease=lease,
        )
        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        self.assertEqual(result.error_category.value, "timeout")
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.QUEUE)
        self.assertEqual(lease.snapshot().phase, TimeoutPhase.QUEUE)

    def test_browser_tier_marks_boot_before_driver_then_work(self) -> None:
        lease = WorkLease.tracking_only()
        _PhaseProbeDriver.lease = lease
        _PhaseProbeDriver.construction_phase = None
        result = _execute(
            scrape_request(
                execution_mode=ExecutionMode.BROWSER,
                navigation_mode=NavigationMode.GET,
                max_retries=0,
            ),
            lease=lease,
            request_id="req-boot-mark",
            Driver=_PhaseProbeDriver,
        )
        self.assertIsInstance(result, ScrapeSuccess)
        self.assertEqual(_PhaseProbeDriver.construction_phase, TimeoutPhase.BOOT)
        _snap_eq(
            self,
            lease.snapshot(),
            phase=TimeoutPhase.WORK,
            attempts=1,
            strategy=NavigationMode.GET,
            tier=ExecutionTier.BROWSER_DRIVER,
        )

    def test_request_tier_marks_work_with_attempt(self) -> None:
        lease = WorkLease.tracking_only()
        result = _execute(
            scrape_request(url=_URL, execution_mode=ExecutionMode.REQUEST),
            lease=lease,
            request_id="req-http-mark",
            Request=fake_request_cls(html=_HTML, url=f"{_URL}/"),
        )
        self.assertIsInstance(result, ScrapeSuccess)
        _snap_eq(
            self,
            lease.snapshot(),
            phase=TimeoutPhase.WORK,
            attempts=1,
            tier=ExecutionTier.HTTP_REQUEST,
        )

    def test_request_tier_timeout_exception_sets_phase(self) -> None:
        class BoomRequest:
            def get(self, *_a: object, **_k: object) -> None:
                raise TimeoutError("HTTP read timeout")

            def close(self) -> None:
                return None

        lease = WorkLease.tracking_only()
        result = _execute(
            scrape_request(url=_URL, execution_mode=ExecutionMode.REQUEST),
            lease=lease,
            request_id="req-http-timeout",
            Request=BoomRequest,
        )
        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        self.assertEqual(result.error_category.value, "timeout")
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.WORK)
        self.assertEqual(result.diagnostics.execution_tier, ExecutionTier.HTTP_REQUEST)
        self.assertEqual(lease.snapshot().phase, TimeoutPhase.WORK)

    def test_browser_tier_timeout_exception_sets_phase(self) -> None:
        class BoomDriver(_PhaseProbeDriver):
            def get(self, *_a: object, **_k: object) -> None:
                raise TimeoutError("navigation timeout")

        lease = WorkLease.tracking_only()
        BoomDriver.lease = lease
        result = _execute(
            scrape_request(
                execution_mode=ExecutionMode.BROWSER,
                navigation_mode=NavigationMode.GET,
                max_retries=0,
            ),
            lease=lease,
            request_id="req-browser-timeout",
            Driver=BoomDriver,
        )
        self.assertIsInstance(result, ScrapeError)
        assert isinstance(result, ScrapeError)
        self.assertEqual(result.error_category.value, "timeout")
        self.assertEqual(result.diagnostics.timeout_phase, TimeoutPhase.WORK)
        self.assertEqual(result.diagnostics.strategy_used, NavigationMode.GET)


if __name__ == "__main__":
    unittest.main()

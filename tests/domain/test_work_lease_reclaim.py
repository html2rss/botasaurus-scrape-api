"""WorkLease reclaim on deadline frees host slot; Future.cancel is not reclaim."""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.config import get_settings
from app.engine.work_lease import HostConcurrencyGate, WorkLease
from app.schemas.enums import TimeoutPhase


def _fast_settings(*, max_per_host: int = 1, timeout_seconds: int = 1):
    return get_settings().model_copy(
        update={
            "scrape_timeout_seconds": timeout_seconds,
            "scrape_work_timeout_seconds": timeout_seconds,
            "scrape_max_per_host": max_per_host,
        }
    )


class WorkLeaseReclaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_invokes_reclaim_and_releases_host_slot(self) -> None:
        settings = _fast_settings(max_per_host=1, timeout_seconds=1)
        gate = HostConcurrencyGate(1)
        executor = ThreadPoolExecutor(max_workers=1)
        lease = WorkLease(settings=settings, executor=executor, host_gate=gate)
        reclaimed = threading.Event()
        release_work = threading.Event()

        def hung_work() -> str:
            release_work.wait(timeout=5)
            return "done"

        lease.register_reclaim(reclaimed.set)
        lease.register_reclaim(release_work.set)

        with self.assertRaises(TimeoutError):
            await lease.run(host="example.com", work=hung_work)

        self.assertTrue(reclaimed.wait(timeout=1))
        # Host slot must be free so a follow-up admit succeeds immediately.
        self.assertTrue(gate.acquire("example.com", timeout=0.1))
        gate.release("example.com")
        executor.shutdown(wait=False, cancel_futures=True)

    async def test_host_admit_timeout_marks_queue(self) -> None:
        settings = _fast_settings(max_per_host=1, timeout_seconds=1)
        gate = HostConcurrencyGate(1)
        self.assertTrue(gate.acquire("busy.example", timeout=0.1))
        executor = ThreadPoolExecutor(max_workers=1)
        lease = WorkLease(settings=settings, executor=executor, host_gate=gate)

        with self.assertRaises(TimeoutError):
            await lease.run(host="busy.example", work=lambda: "never")

        self.assertEqual(lease.snapshot().phase, TimeoutPhase.QUEUE)
        gate.release("busy.example")
        executor.shutdown(wait=False, cancel_futures=True)

    async def test_successful_run_does_not_reclaim(self) -> None:
        settings = _fast_settings(max_per_host=2, timeout_seconds=5)
        gate = HostConcurrencyGate(2)
        executor = ThreadPoolExecutor(max_workers=1)
        lease = WorkLease(settings=settings, executor=executor, host_gate=gate)
        reclaim_calls = 0

        def mark_reclaim() -> None:
            nonlocal reclaim_calls
            reclaim_calls += 1

        lease.register_reclaim(mark_reclaim)
        result = await lease.run(host="ok.example", work=lambda: "ok")
        self.assertEqual(result, "ok")
        self.assertEqual(reclaim_calls, 0)
        self.assertFalse(lease.aborted)
        executor.shutdown(wait=False, cancel_futures=True)

    async def test_reclaim_sets_aborted_before_hooks(self) -> None:
        settings = _fast_settings(max_per_host=1, timeout_seconds=5)
        lease = WorkLease.tracking_only(settings)
        seen_aborted = False

        def hook() -> None:
            nonlocal seen_aborted
            seen_aborted = lease.aborted

        lease.register_reclaim(hook)
        lease.reclaim()
        self.assertTrue(lease.aborted)
        self.assertTrue(seen_aborted)


if __name__ == "__main__":
    unittest.main()

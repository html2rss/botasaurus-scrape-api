"""Unit tests for WarmDriverPool and DriverFingerprint."""

from __future__ import annotations

import io
import logging
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.engine.warm_pool import (
    DriverFingerprint,
    WarmDriverPool,
    cgroup_memory_under_pressure,
)
from app.logging_config import get_logger
from tests.support.factories import scrape_request
from tests.support.fakes import FakeDriver, FakeDriverTab


class TrackingDriver(FakeDriver):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.closed = False
        self.build_thread = threading.current_thread()

    def close(self) -> None:
        self.closed = True


def _fp(**overrides: object) -> DriverFingerprint:
    payload = scrape_request(
        execution_mode="browser",
        headless=overrides.get("headless", True),
        proxy=overrides.get("proxy"),
        block_images=overrides.get("block_images", True),
        block_images_and_css=overrides.get("block_images_and_css", False),
        wait_for_complete_page_load=overrides.get("wait_for_complete_page_load", True),
        user_agent=overrides.get("user_agent"),
        window_size=overrides.get("window_size"),
        lang=overrides.get("lang"),
    )
    return DriverFingerprint.from_request(payload)


class WarmPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tmp.name)
        self.built: list[TrackingDriver] = []
        self.factory_threads: list[threading.Thread] = []
        self.factory_started = threading.Event()
        self.factory_release = threading.Event()
        self.factory_release.set()
        self.spare_ready = threading.Event()
        self.clock_value = 1000.0
        self.pressure = False

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _clock(self) -> float:
        return self.clock_value

    def _factory(
        self, fingerprint: DriverFingerprint, spare_dir: Path
    ) -> TrackingDriver:
        del fingerprint, spare_dir
        self.factory_threads.append(threading.current_thread())
        self.factory_started.set()
        self.factory_release.wait(timeout=5)
        driver = TrackingDriver()
        self.built.append(driver)
        return driver

    def _health_ok(self, driver: TrackingDriver) -> FakeDriverTab:
        del driver
        return FakeDriverTab()

    def _health_dead(self, driver: TrackingDriver) -> None:
        del driver
        return None

    def _pool(self, **kwargs: object) -> WarmDriverPool:
        original_factory = kwargs.pop("driver_factory", self._factory)

        def wrapping_factory(
            fingerprint: DriverFingerprint, spare_dir: Path
        ) -> TrackingDriver:
            driver = original_factory(fingerprint, spare_dir)  # type: ignore[operator]
            assert isinstance(driver, TrackingDriver)
            self.spare_ready.set()
            return driver

        defaults: dict[str, object] = {
            "runtime_root": self.runtime_root,
            "idle_ttl_seconds": 600,
            "min_refill_seconds": 0,
            "driver_factory": wrapping_factory,
            "health_check": self._health_ok,
            "memory_pressure": lambda: self.pressure,
            "clock": self._clock,
            "headless_only": False,
            "join_timeout_seconds": 2.0,
        }
        defaults.update(kwargs)
        self.spare_ready.clear()
        return WarmDriverPool(**defaults)  # type: ignore[arg-type]

    def _wait_spare(self, pool: WarmDriverPool, timeout: float = 2.0) -> None:
        if not self.spare_ready.wait(timeout=timeout):
            self.fail("spare factory did not complete")
        # Factory returns before slot is stored; brief spin for store under lock.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pool.ready_spare_dirs():
                return
            time.sleep(0.001)
        self.fail("spare did not appear in pool")

    def test_fingerprint_normalizes_window_size_and_ua(self) -> None:
        req = scrape_request(
            execution_mode="browser",
            headers={"User-Agent": "From-Header/1.0"},
            window_size={"width": 1280, "height": 720},
        )
        fp = DriverFingerprint.from_request(req)
        self.assertEqual(fp.user_agent, "From-Header/1.0")
        self.assertEqual(fp.window_size, (1280, 720))
        self.assertNotIn("From-Header", repr(fp))
        self.assertIn("hash=", repr(fp))

    def test_take_hit_miss_and_one_shot(self) -> None:
        pool = self._pool()
        fp = _fp()
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)

        hit = pool.take(fp)
        self.assertIsNotNone(hit)
        assert hit is not None
        driver, spare_dir = hit
        self.assertTrue(spare_dir.name.startswith("spare-"))
        self.assertIs(driver, self.built[0])
        self.assertEqual(pool.ready_spare_dirs(), set())
        self.assertEqual(pool.live_spare_dirs(), {spare_dir})

        self.assertIsNone(pool.take(fp))
        pool.release_adopted(spare_dir)
        self.assertEqual(pool.live_spare_dirs(), set())
        pool.shutdown()

    def test_take_miss_on_fingerprint_mismatch(self) -> None:
        pool = self._pool()
        fp_a = _fp(user_agent="A")
        fp_b = _fp(user_agent="B")
        pool.notify_scrape_finished(fp_a)
        self._wait_spare(pool)
        self.assertIsNone(pool.take(fp_b))
        self.assertTrue(pool.live_spare_dirs())
        pool.shutdown()

    def test_unhealthy_spare_is_reaped_not_handed_out(self) -> None:
        pool = self._pool(health_check=self._health_dead)
        fp = _fp()
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        self.assertIsNone(pool.take(fp))
        self.assertTrue(self.built[0].closed)
        self.assertEqual(pool.live_spare_dirs(), set())
        pool.shutdown()

    def test_idle_ttl_reaps_spare(self) -> None:
        closed = threading.Event()

        class ClosingDriver(TrackingDriver):
            def close(self) -> None:
                super().close()
                closed.set()

        def factory(fingerprint: DriverFingerprint, spare_dir: Path) -> ClosingDriver:
            del fingerprint, spare_dir
            self.factory_threads.append(threading.current_thread())
            driver = ClosingDriver()
            self.built.append(driver)
            return driver

        pool = self._pool(idle_ttl_seconds=10, driver_factory=factory)
        fp = _fp()
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        self.clock_value += 11
        # Wake refill loop via public notify (also re-queues same fingerprint).
        pool.notify_scrape_finished(fp)
        self.assertTrue(closed.wait(timeout=2))
        self.assertTrue(self.built[0].closed)
        # notify also queues refill, so live_spare_dirs may already hold the
        # in-build or ready replacement — only the reaped driver must be gone.
        pool.shutdown()

    def test_shutdown_closes_spare_and_leak_audit(self) -> None:
        pool = self._pool()
        fp = _fp()
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        pool.shutdown()
        self.assertTrue(self.built[0].closed)
        self.assertEqual(pool.build_count, pool.close_count)

    def test_shutdown_during_build_closes_late_driver(self) -> None:
        self.factory_release.clear()
        pool = self._pool()
        fp = _fp()
        pool.notify_scrape_finished(fp)
        self.assertTrue(self.factory_started.wait(timeout=2))
        # Join times out while factory is blocked; release afterward.
        shutdown_done = threading.Event()

        def do_shutdown() -> None:
            pool.shutdown()
            shutdown_done.set()

        threading.Thread(target=do_shutdown, daemon=True).start()
        self.assertTrue(shutdown_done.wait(timeout=5))
        self.factory_release.set()
        closed = threading.Event()

        def wait_closed() -> None:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if self.built and self.built[0].closed:
                    closed.set()
                    return
                time.sleep(0.01)

        wait_closed()
        self.assertTrue(closed.wait(timeout=0))
        self.assertEqual(pool.live_spare_dirs(), set())
        self.assertGreaterEqual(pool.close_count, pool.build_count)

    def test_concurrent_take_and_refill_uses_barrier(self) -> None:
        pool = self._pool()
        fp = _fp()
        barrier = threading.Barrier(2)
        results: list[tuple[TrackingDriver, Path] | None] = []

        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        self.spare_ready.clear()

        def taker() -> None:
            barrier.wait(timeout=2)
            taken = pool.take(fp)
            if taken is None:
                results.append(None)
            else:
                driver, spare_dir = taken
                assert isinstance(driver, TrackingDriver)
                results.append((driver, spare_dir))

        def notifier() -> None:
            barrier.wait(timeout=2)
            pool.notify_scrape_finished(fp)

        t1 = threading.Thread(target=taker)
        t2 = threading.Thread(target=notifier)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0])
        self._wait_spare(pool)
        pool.shutdown()

    def test_min_refill_backoff(self) -> None:
        pool = self._pool(min_refill_seconds=30)
        fp = _fp()
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        first = pool.take(fp)
        self.assertIsNotNone(first)
        assert first is not None
        _, adopted = first
        self.spare_ready.clear()
        pool.notify_scrape_finished(fp)
        self.assertFalse(self.spare_ready.wait(timeout=0.1))
        self.assertEqual(pool.ready_spare_dirs(), set())
        self.assertEqual(pool.live_spare_dirs(), {adopted})
        self.assertEqual(len(self.built), 1)
        self.clock_value += 31
        # Deferred wake uses Event timeout (wall clock); nudge via notify.
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        self.assertEqual(len(self.built), 2)
        pool.release_adopted(adopted)
        pool.shutdown()

    def test_memory_pressure_skips_refill(self) -> None:
        self.pressure = True
        pool = self._pool()
        fp = _fp()
        pool.notify_scrape_finished(fp)
        self.assertFalse(self.spare_ready.wait(timeout=0.15))
        self.assertEqual(pool.ready_spare_dirs(), set())
        self.assertEqual(len(self.built), 0)
        # Clearing pressure + advancing past deferred wake should refill.
        # notify_scrape_finished only re-sets desired + wakes (injected clock
        # vs Event.wait wall time cannot auto-fire the deferred timeout).
        self.pressure = False
        self.clock_value += 2.0
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        self.assertEqual(len(self.built), 1)
        pool.shutdown()

    def test_prune_after_take_keeps_adopted_spare(self) -> None:
        pool = self._pool()
        fp = _fp()
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        hit = pool.take(fp)
        self.assertIsNotNone(hit)
        assert hit is not None
        _, spare_dir = hit
        (spare_dir / "marker").write_text("in-use", encoding="utf-8")

        from app.infra.runtime_cleanup import prune_orphan_runtime_dirs

        removed = prune_orphan_runtime_dirs(
            self.runtime_root,
            set(),
            protected_dirs=pool.live_spare_dirs(),
        )
        self.assertEqual(removed, 0)
        self.assertTrue(spare_dir.is_dir())
        self.assertTrue((spare_dir / "marker").exists())

        pool.release_adopted(spare_dir)
        shutil.rmtree(spare_dir, ignore_errors=True)
        pool.shutdown()

    def test_building_dir_is_protected(self) -> None:
        self.factory_release.clear()
        pool = self._pool()
        fp = _fp()
        pool.notify_scrape_finished(fp)
        self.assertTrue(self.factory_started.wait(timeout=2))
        protected = pool.live_spare_dirs()
        self.assertEqual(len(protected), 1)
        building = next(iter(protected))
        self.assertTrue(building.name.startswith("spare-"))
        self.assertEqual(pool.ready_spare_dirs(), set())
        self.factory_release.set()
        self._wait_spare(pool)
        self.assertEqual(pool.ready_spare_dirs(), {building})
        pool.shutdown()

    def test_factory_runs_on_dedicated_daemon_thread(self) -> None:
        pool = self._pool()
        request_executor_thread = threading.current_thread()
        fp = _fp()
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        self.assertEqual(len(self.factory_threads), 1)
        self.assertIsNot(self.factory_threads[0], request_executor_thread)
        self.assertTrue(self.factory_threads[0].daemon)
        self.assertEqual(self.factory_threads[0].name, "warm-driver-pool-refill")
        pool.shutdown()

    def test_log_redaction_excludes_proxy_and_ua(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.INFO)
        logger = get_logger()
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            pool = self._pool()
            fp = _fp(
                proxy="http://user:secret@proxy.example:8080",
                user_agent="SecretUA/9",
            )
            pool.notify_scrape_finished(fp)
            self._wait_spare(pool)
            pool.take(fp)
            pool.shutdown()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        logs = stream.getvalue()
        self.assertNotIn("secret", logs.lower())
        self.assertNotIn("SecretUA", logs)
        self.assertNotIn("proxy.example", logs)
        self.assertIn("fingerprint_hash=", logs)

    def test_orphan_spare_dirs_cleaned_at_init(self) -> None:
        orphan = self.runtime_root / "spare-orphan"
        orphan.mkdir()
        (orphan / "profile").mkdir()
        pool = self._pool()
        self.assertFalse(orphan.exists())
        pool.shutdown()

    def test_headless_only_gate_rejects_headed_fingerprint(self) -> None:
        pool = self._pool(headless_only=True)
        fp = _fp(headless=False)
        pool.notify_scrape_finished(fp)
        self.assertFalse(self.spare_ready.wait(timeout=0.15))
        self.assertIsNone(pool.take(fp))
        self.assertEqual(len(self.built), 0)
        pool.shutdown()

    def test_cgroup_memory_under_pressure_best_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "memory.current"
            maximum = root / "memory.max"
            current.write_text("80")
            maximum.write_text("100")
            self.assertTrue(
                cgroup_memory_under_pressure(
                    ratio=0.7, current_path=current, max_path=maximum
                )
            )
            maximum.write_text("max")
            self.assertFalse(
                cgroup_memory_under_pressure(
                    ratio=0.7, current_path=current, max_path=maximum
                )
            )


if __name__ == "__main__":
    unittest.main()

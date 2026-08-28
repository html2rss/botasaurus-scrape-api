"""Phase 2 wiring tests for prewarmed browser handoff."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from app.config import get_settings
from app.engine import ScraperEngine
from app.engine.session import ScrapeSession
from app.engine.warm_pool import (
    DriverFactory,
    DriverFingerprint,
    HealthCheck,
    WarmDriverPool,
)
from app.infra.runtime_cleanup import prune_orphan_runtime_dirs
from app.schemas.response import ScrapeError, ScrapeSuccess
from tests.support.factories import scrape_request
from tests.support.fakes import HealthCheckStub, InstrumentedDriver, TrackingDriver


class WarmWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        TrackingDriver.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.tmp.name)
        self.built_via_pool: list[TrackingDriver] = []
        self.spare_ready = threading.Event()
        self.factory_started = threading.Event()
        self.factory_release = threading.Event()
        self.factory_release.set()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _factory(
        self, fingerprint: DriverFingerprint, spare_dir: Path
    ) -> InstrumentedDriver:
        del fingerprint, spare_dir
        self.factory_started.set()
        self.factory_release.wait(timeout=5)
        driver = InstrumentedDriver()
        self.built_via_pool.append(driver)
        self.spare_ready.set()
        return driver

    def _health_ok(self, _driver: object) -> object:
        return HealthCheckStub()(_driver)  # type: ignore[arg-type]

    def _attach_pool(self, engine: ScraperEngine) -> WarmDriverPool:
        pool = WarmDriverPool(
            runtime_root=engine.runtime_root,
            idle_ttl_seconds=600,
            min_refill_seconds=0,
            driver_factory=cast(DriverFactory, self._factory),
            health_check=cast(HealthCheck, self._health_ok),
            memory_pressure=lambda: False,
            join_timeout_seconds=2.0,
        )
        engine.warm_pool = pool
        return pool

    def _wait_spare(self, pool: WarmDriverPool, timeout: float = 2.0) -> None:
        self.assertTrue(self.spare_ready.wait(timeout=timeout))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pool.ready_spare_dirs():
                return
            time.sleep(0.001)
        self.fail("spare missing")

    def test_warm_hit_skips_driver_factory(self) -> None:
        engine = ScraperEngine(settings=get_settings(), runtime_root=self.runtime_root)
        pool = self._attach_pool(engine)
        fp = DriverFingerprint.from_request(
            scrape_request(execution_mode="browser", headless=True)
        )
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        cold_builds = 0

        def counting_driver(*args: object, **kwargs: object) -> TrackingDriver:
            nonlocal cold_builds
            cold_builds += 1
            return TrackingDriver(*args, **kwargs)

        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            headless=True,
        )
        with patch("botasaurus.browser.Driver", counting_driver):
            result = engine.execute(payload)

        self.assertIsInstance(result, ScrapeSuccess)
        self.assertEqual(cold_builds, 0)
        self.assertEqual(len(self.built_via_pool), 1)
        pool.shutdown()

    def test_warm_hit_applies_request_cookies_and_headers(self) -> None:
        engine = ScraperEngine(settings=get_settings(), runtime_root=self.runtime_root)
        pool = self._attach_pool(engine)
        payload_a = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            headless=True,
            cookies={"a": "1"},
            headers={"X-Test": "first"},
        )
        with patch("botasaurus.browser.Driver", InstrumentedDriver):
            result_a = engine.execute(payload_a, request_id="req-a")

        self.assertIsInstance(result_a, ScrapeSuccess)
        self._wait_spare(pool)

        payload_b = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            headless=True,
            cookies={"b": "2"},
            headers={"X-Test": "second"},
        )
        cold_builds = 0

        def counting_instrumented(
            *args: object, **kwargs: object
        ) -> InstrumentedDriver:
            nonlocal cold_builds
            cold_builds += 1
            return InstrumentedDriver(*args, **kwargs)

        with patch("botasaurus.browser.Driver", counting_instrumented):
            result_b = engine.execute(payload_b, request_id="req-b")

        self.assertIsInstance(result_b, ScrapeSuccess)
        self.assertEqual(cold_builds, 0)

        warm_driver = self.built_via_pool[0]
        self.assertEqual(len(warm_driver.state.cookies), 1)
        cookie_batch = warm_driver.state.cookies[0]
        self.assertEqual(len(cookie_batch), 1)
        self.assertEqual(cookie_batch[0]["name"], "b")
        self.assertEqual(cookie_batch[0]["value"], "2")
        self.assertEqual(len(warm_driver.state.headers), 1)
        self.assertEqual(warm_driver.state.headers[0]["X-Test"], "second")
        pool.shutdown()

    def test_warm_miss_uses_cold_driver(self) -> None:
        engine = ScraperEngine(settings=get_settings(), runtime_root=self.runtime_root)
        pool = self._attach_pool(engine)
        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            headless=True,
        )
        with patch("botasaurus.browser.Driver", TrackingDriver):
            result = engine.execute(payload)

        self.assertIsInstance(result, ScrapeSuccess)
        self.assertEqual(len(TrackingDriver.instances), 1)
        # Refill after exit builds a spare for the fingerprint.
        self._wait_spare(pool)
        pool.shutdown()

    def test_adopted_profile_dir_deleted_on_exit(self) -> None:
        engine = ScraperEngine(settings=get_settings(), runtime_root=self.runtime_root)
        pool = self._attach_pool(engine)
        fp = DriverFingerprint.from_request(
            scrape_request(execution_mode="browser", headless=True)
        )
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        spare_dirs = set(pool.live_spare_dirs())
        self.assertEqual(len(spare_dirs), 1)
        spare = next(iter(spare_dirs))

        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            headless=True,
        )
        with patch("botasaurus.browser.Driver", TrackingDriver):
            result = engine.execute(payload)

        self.assertIsInstance(result, ScrapeSuccess)
        self.assertFalse(spare.exists())
        pool.shutdown()

    def test_isolation_no_shared_driver_across_requests(self) -> None:
        engine = ScraperEngine(settings=get_settings(), runtime_root=self.runtime_root)
        pool = self._attach_pool(engine)
        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            headless=True,
        )
        drivers: list[TrackingDriver] = []

        with patch("botasaurus.browser.Driver", TrackingDriver):
            engine.execute(payload, request_id="req-a")
            drivers.extend(TrackingDriver.instances)
            self._wait_spare(pool)
            TrackingDriver.instances = []
            engine.execute(payload, request_id="req-b")
            drivers.extend(TrackingDriver.instances)
            drivers.extend(self.built_via_pool)

        self.assertGreaterEqual(len(drivers), 2)
        self.assertIsNot(drivers[0], drivers[1])
        pool.shutdown()

    def test_adoption_failure_closes_spare(self) -> None:
        engine = ScraperEngine(settings=get_settings(), runtime_root=self.runtime_root)
        pool = self._attach_pool(engine)
        fp = DriverFingerprint.from_request(
            scrape_request(execution_mode="browser", headless=True)
        )
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        spare_driver = self.built_via_pool[0]

        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            headless=True,
        )

        def boom_prepare(self: ScrapeSession) -> None:
            del self
            raise OSError(28, "No space left on device")

        with (
            patch.object(ScrapeSession, "prepare_runtime_dir", boom_prepare),
            patch("botasaurus.browser.Driver", TrackingDriver),
        ):
            result = engine.execute(payload)

        self.assertTrue(spare_driver.closed)
        assert isinstance(result, ScrapeError)
        self.assertIn("runtime storage full", result.error)
        pool.shutdown()

    def test_leak_audit_close_count_matches_build_count(self) -> None:
        engine = ScraperEngine(settings=get_settings(), runtime_root=self.runtime_root)
        pool = self._attach_pool(engine)
        payload = scrape_request(
            execution_mode="browser",
            navigation_mode="get",
            max_retries=0,
            headless=True,
        )
        with patch("botasaurus.browser.Driver", TrackingDriver):
            engine.execute(payload)
        self._wait_spare(pool)
        pool.shutdown()
        self.assertEqual(pool.build_count, pool.close_count)

    def test_prune_keeps_live_spare(self) -> None:
        spare = self.runtime_root / "spare-live"
        spare.mkdir()
        orphan = self.runtime_root / "orphan-req"
        orphan.mkdir()
        protected = {spare}
        removed = prune_orphan_runtime_dirs(
            self.runtime_root, set(), protected_dirs=protected
        )
        self.assertEqual(removed, 1)
        self.assertTrue(spare.is_dir())
        self.assertFalse(orphan.exists())

    def test_prune_after_take_keeps_adopted_spare(self) -> None:
        engine = ScraperEngine(settings=get_settings(), runtime_root=self.runtime_root)
        pool = self._attach_pool(engine)
        fp = DriverFingerprint.from_request(
            scrape_request(execution_mode="browser", headless=True)
        )
        pool.notify_scrape_finished(fp)
        self._wait_spare(pool)
        hit = pool.take(fp)
        self.assertIsNotNone(hit)
        assert hit is not None
        driver, spare_dir = hit
        (spare_dir / "marker").write_text("in-use", encoding="utf-8")

        removed = engine.prune_runtime_dirs()
        self.assertEqual(removed, 0)
        self.assertTrue(spare_dir.is_dir())
        self.assertTrue((spare_dir / "marker").exists())

        driver.close()
        shutil.rmtree(spare_dir, ignore_errors=True)
        pool.release_adopted(spare_dir)
        pool.shutdown()

    def test_prune_protects_building_spare_dir(self) -> None:
        engine = ScraperEngine(settings=get_settings(), runtime_root=self.runtime_root)
        pool = self._attach_pool(engine)
        fp = DriverFingerprint.from_request(
            scrape_request(execution_mode="browser", headless=True)
        )
        self.factory_release.clear()
        self.factory_started.clear()
        pool.notify_scrape_finished(fp)
        self.assertTrue(self.factory_started.wait(timeout=2))
        protected = pool.live_spare_dirs()
        self.assertEqual(len(protected), 1)
        building_dir = next(iter(protected))
        self.assertTrue(building_dir.name.startswith("spare-"))

        removed = engine.prune_runtime_dirs()
        self.assertEqual(removed, 0)
        self.assertTrue(building_dir.is_dir())

        self.factory_release.set()
        self._wait_spare(pool)
        pool.shutdown()


if __name__ == "__main__":
    unittest.main()

"""Engine singleton and per-request isolation regression tests."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import get_settings
from app.engine import ScraperEngine
from app.engine.session import ScrapeSession
from app.exceptions import RequestIdCollisionError
from tests.support.fakes import fake_request_cls
from tests.support.http import test_client


class EngineSingletonTests(unittest.TestCase):
    def test_app_state_shares_one_engine_and_executor(self):
        with test_client() as client:
            engine_a = client.app.state.engine
            engine_b = client.app.state.engine
            executor_a = client.app.state.executor
            executor_b = client.app.state.executor

        self.assertIs(engine_a, engine_b)
        self.assertIs(executor_a, executor_b)


class IsolationRegressionTests(unittest.TestCase):
    INBOUND_ID_A = "550e8400-e29b-41d4-a716-446655440001"
    INBOUND_ID_B = "550e8400-e29b-41d4-a716-446655440002"
    COLLISION_ID = "550e8400-e29b-41d4-a716-446655440000"

    def test_concurrent_scrapes_use_distinct_runtime_dirs(self):
        runtime_dirs: list[Path] = []
        gate = threading.Event()
        release = threading.Event()
        original_enter = ScrapeSession.__enter__

        def tracking_enter(self: ScrapeSession) -> ScrapeSession:
            session = original_enter(self)
            runtime_dirs.append(session.runtime_dir)
            if len(runtime_dirs) == 1:
                gate.set()
                release.wait(timeout=5)
            return session

        with (
            patch.object(ScrapeSession, "__enter__", tracking_enter),
            patch("botasaurus.request.Request", fake_request_cls()),
            test_client() as client,
        ):
            first = threading.Thread(
                target=lambda: client.post(
                    "/scrape",
                    json={"url": "https://example.com", "execution_mode": "request"},
                    headers={"X-Request-Id": self.INBOUND_ID_A},
                )
            )
            second = threading.Thread(
                target=lambda: client.post(
                    "/scrape",
                    json={"url": "https://example.com", "execution_mode": "request"},
                    headers={"X-Request-Id": self.INBOUND_ID_B},
                )
            )
            first.start()
            self.assertTrue(gate.wait(timeout=5))
            second.start()
            release.set()
            first.join(timeout=10)
            second.join(timeout=10)

        self.assertEqual(len(runtime_dirs), 2)
        self.assertNotEqual(runtime_dirs[0], runtime_dirs[1])

    def test_duplicate_request_id_while_active_returns_502(self):
        active = threading.Event()
        release = threading.Event()
        original_enter = ScrapeSession.__enter__
        collision_id = self.COLLISION_ID

        def slow_enter(self: ScrapeSession) -> ScrapeSession:
            session = original_enter(self)
            if self.request_id == collision_id:
                active.set()
                release.wait(timeout=5)
            return session

        with (
            patch.object(ScrapeSession, "__enter__", slow_enter),
            patch("botasaurus.request.Request", fake_request_cls()),
            test_client() as client,
        ):
            first = threading.Thread(
                target=lambda: client.post(
                    "/scrape",
                    json={"url": "https://example.com", "execution_mode": "request"},
                    headers={"X-Request-Id": self.COLLISION_ID},
                )
            )
            first.start()
            self.assertTrue(active.wait(timeout=5))

            response = client.post(
                "/scrape",
                json={"url": "https://example.com", "execution_mode": "request"},
                headers={"X-Request-Id": self.COLLISION_ID},
            )
            release.set()
            first.join(timeout=10)

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["diagnostics"]["request_id"], self.COLLISION_ID)
        self.assertEqual(body["error_category"], "navigation_error")

    def test_runtime_dir_removed_after_scrape_completes(self):
        captured_dir: Path | None = None
        original_exit = ScrapeSession.__exit__

        def capture_exit(
            self: ScrapeSession,
            exc_type: object,
            exc_val: object,
            exc_tb: object,
        ) -> bool | None:
            nonlocal captured_dir
            captured_dir = self.runtime_dir
            return original_exit(self, exc_type, exc_val, exc_tb)  # type: ignore[arg-type]

        with (
            patch.object(ScrapeSession, "__exit__", capture_exit),
            patch("botasaurus.request.Request", fake_request_cls()),
            test_client() as client,
        ):
            response = client.post(
                "/scrape",
                json={"url": "https://example.com", "execution_mode": "request"},
                headers={"X-Request-Id": "req-cleanup"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(captured_dir)
        assert captured_dir is not None
        self.assertFalse(captured_dir.exists())

    def test_shared_engine_tracks_active_request_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            request_id = "req-active-track"
            engine.register_request_id(request_id)
            try:
                with self.assertRaises(RequestIdCollisionError):
                    engine.register_request_id(request_id)
            finally:
                engine.unregister_request_id(request_id)
            engine.register_request_id(request_id)
            engine.unregister_request_id(request_id)

    def test_session_unregisters_when_prepare_runtime_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = ScraperEngine(settings=get_settings(), runtime_root=Path(tmp))
            request_id = "req-prepare-fail"
            with (
                patch.object(
                    engine,
                    "prepare_runtime_for_request",
                    side_effect=OSError("boom"),
                ),
                self.assertRaises(OSError),
                ScrapeSession(engine, request_id),
            ):
                pass
            engine.register_request_id(request_id)
            engine.unregister_request_id(request_id)

    def test_prune_keeps_registered_request_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            engine = ScraperEngine(settings=get_settings(), runtime_root=runtime_root)
            orphan = runtime_root / "orphan"
            orphan.mkdir()
            live = runtime_root / "live-req"
            live.mkdir()
            engine.register_request_id("live-req")
            try:
                removed = engine.prune_runtime_dirs()
            finally:
                engine.unregister_request_id("live-req")

            self.assertEqual(removed, 1)
            self.assertFalse(orphan.exists())
            self.assertTrue(live.is_dir())


if __name__ == "__main__":
    unittest.main()

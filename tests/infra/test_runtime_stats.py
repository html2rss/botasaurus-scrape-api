"""Tests for runtime preflight probes and stats heartbeat snapshot."""

from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.infra.cgroup import CgroupMemorySnapshot, read_cgroup_memory
from app.infra.runtime_stats import (
    RuntimeStatsReporter,
    collect_runtime_snapshot,
    count_chromium_procs,
    home_is_writable,
    run_runtime_preflight,
)
from app.logging_config import LOGGER_NAME, JsonFormatter, setup_logging


class CgroupTests(unittest.TestCase):
    def test_read_cgroup_memory_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "memory.current"
            maximum = root / "memory.max"
            current.write_text("70", encoding="utf-8")
            maximum.write_text("100", encoding="utf-8")
            snap = read_cgroup_memory(current_path=current, max_path=maximum)
            self.assertEqual(snap.current, 70)
            self.assertEqual(snap.maximum, 100)
            self.assertAlmostEqual(snap.ratio or 0.0, 0.7)
            self.assertTrue(snap.under_pressure(ratio=0.7))

    def test_unlimited_max_has_no_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "memory.current"
            maximum = root / "memory.max"
            current.write_text("1", encoding="utf-8")
            maximum.write_text("max", encoding="utf-8")
            snap = read_cgroup_memory(current_path=current, max_path=maximum)
            self.assertIsNone(snap.maximum)
            self.assertIsNone(snap.ratio)
            self.assertFalse(snap.under_pressure())


class RuntimeStatsTests(unittest.TestCase):
    def test_count_chromium_procs_from_fake_proc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "1").mkdir()
            (root / "1" / "comm").write_text("chromium\n", encoding="utf-8")
            (root / "2").mkdir()
            (root / "2" / "comm").write_text("python3\n", encoding="utf-8")
            (root / "3").mkdir()
            (root / "3" / "comm").write_text("chrome\n", encoding="utf-8")
            self.assertEqual(count_chromium_procs(proc_root=root), 2)

    def test_home_writable_and_preflight_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            runtime = Path(tmp) / "scrape"
            home.mkdir()
            self.assertTrue(home_is_writable(home=home))
            with patch(
                "app.infra.runtime_stats.resolve_chromium_binary",
                return_value=Path("/usr/bin/chromium"),
            ):
                self.assertTrue(run_runtime_preflight(runtime_root=runtime, home=home))

    def test_preflight_fails_when_home_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            home.chmod(0o500)
            runtime = Path(tmp) / "scrape"
            try:
                with patch(
                    "app.infra.runtime_stats.resolve_chromium_binary",
                    return_value=Path("/usr/bin/chromium"),
                ):
                    self.assertFalse(
                        run_runtime_preflight(runtime_root=runtime, home=home)
                    )
            finally:
                home.chmod(0o700)

    def test_reporter_emits_runtime_stats_json(self) -> None:
        setup_logging(log_format="json")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logging.getLogger(LOGGER_NAME).addHandler(handler)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "req-1").mkdir()
            reporter = RuntimeStatsReporter(
                interval_seconds=0,
                runtime_root=root,
                active_requests=lambda: 1,
                warm_present=lambda: True,
            )
            try:
                with patch(
                    "app.infra.runtime_stats.read_cgroup_memory",
                    return_value=CgroupMemorySnapshot(current=50, maximum=100),
                ):
                    reporter.emit_once()
            finally:
                logging.getLogger(LOGGER_NAME).removeHandler(handler)
            line = stream.getvalue().strip().splitlines()[-1]
            payload = json.loads(line)
            self.assertEqual(payload["event_name"], "runtime_stats")
            self.assertEqual(payload["active_requests"], 1)
            self.assertEqual(payload["warm_present"], True)
            self.assertEqual(payload["runtime_dirs"], 1)
            self.assertEqual(payload["mem_ratio"], 0.5)

    def test_collect_snapshot_counts_runtime_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "b").mkdir()
            snap = collect_runtime_snapshot(
                runtime_root=root,
                active_requests=2,
                warm_present=False,
                proc_root=Path(tmp) / "empty-proc",
            )
            self.assertEqual(snap.runtime_dirs, 2)
            self.assertEqual(snap.active_requests, 2)
            self.assertFalse(snap.warm_present)


if __name__ == "__main__":
    unittest.main()

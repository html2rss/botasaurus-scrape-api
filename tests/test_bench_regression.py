"""Lightweight guard that the scrape bench harness completes."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


class BenchRegressionTests(unittest.TestCase):
    def test_bench_script_completes_under_generous_ceiling(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "bench_scrape.py"
        env = {**os.environ, "PYTHONPATH": str(repo_root)}
        completed = subprocess.run(
            [sys.executable, str(script), "--runs", "2"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("wall_ms_p50=", completed.stdout)

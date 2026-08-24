# tests/test_runtime_cleanup.py
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.runtime_cleanup import (
    MIN_FREE_BYTES,
    prune_orphan_runtime_dirs,
    runtime_root_low_on_space,
)


class RuntimeCleanupTests(unittest.TestCase):
    def test_prune_orphan_runtime_dirs_removes_inactive_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            orphan = runtime_root / "orphan-req"
            orphan.mkdir()
            (orphan / "profile").mkdir()
            active = runtime_root / "active-req"
            active.mkdir()

            removed = prune_orphan_runtime_dirs(runtime_root, {"active-req"})

            self.assertEqual(removed, 1)
            self.assertFalse(orphan.exists())
            self.assertTrue(active.is_dir())

    def test_prune_orphan_runtime_dirs_noop_when_root_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp) / "missing"
            self.assertEqual(prune_orphan_runtime_dirs(runtime_root, set()), 0)

    def test_runtime_root_low_on_space_uses_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            low_usage = type(
                "Usage",
                (),
                {"total": 10_000_000, "used": 1_000_000, "free": MIN_FREE_BYTES - 1},
            )()
            with patch("app.runtime_cleanup.shutil.disk_usage", return_value=low_usage):
                self.assertTrue(runtime_root_low_on_space(runtime_root))

            ok_usage = type(
                "Usage",
                (),
                {"total": 10_000_000, "used": 1_000_000, "free": MIN_FREE_BYTES + 1},
            )()
            with patch("app.runtime_cleanup.shutil.disk_usage", return_value=ok_usage):
                self.assertFalse(runtime_root_low_on_space(runtime_root))


if __name__ == "__main__":
    unittest.main()

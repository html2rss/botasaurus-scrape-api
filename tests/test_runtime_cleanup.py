from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import get_settings, reset_settings_cache
from app.infra.runtime_cleanup import (
    prune_orphan_runtime_dirs,
    runtime_root_low_on_space,
)

MIN_FREE_BYTES = get_settings().scrape_runtime_min_free_bytes


class RuntimeCleanupTests(unittest.TestCase):
    def setUp(self):
        reset_settings_cache()

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
            with patch(
                "app.infra.runtime_cleanup.shutil.disk_usage", return_value=low_usage
            ):
                self.assertTrue(
                    runtime_root_low_on_space(
                        runtime_root, min_free_bytes=MIN_FREE_BYTES
                    )
                )

            ok_usage = type(
                "Usage",
                (),
                {"total": 10_000_000, "used": 1_000_000, "free": MIN_FREE_BYTES + 1},
            )()
            with patch(
                "app.infra.runtime_cleanup.shutil.disk_usage", return_value=ok_usage
            ):
                self.assertFalse(
                    runtime_root_low_on_space(
                        runtime_root, min_free_bytes=MIN_FREE_BYTES
                    )
                )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("botasaurus_scrape_api")


def runtime_root_low_on_space(
    runtime_root: Path,
    *,
    min_free_bytes: int,
) -> bool:
    """Return True when free space under runtime_root's filesystem is below min_free_bytes."""
    runtime_root.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(runtime_root).free < min_free_bytes


def prune_orphan_runtime_dirs(
    runtime_root: Path,
    active_request_ids: set[str],
    *,
    min_free_bytes: int | None = None,
) -> int:
    """Delete runtime dirs that are not tied to an active request id."""
    del min_free_bytes
    if not runtime_root.is_dir():
        return 0

    removed = 0
    for entry in runtime_root.iterdir():
        if not entry.is_dir() or entry.name in active_request_ids:
            continue
        try:
            shutil.rmtree(entry)
            removed += 1
            logger.info("runtime_dir_pruned path=%s", entry)
        except OSError as exc:
            logger.warning("runtime_dir_prune_failed path=%s error=%s", entry, exc)
    return removed

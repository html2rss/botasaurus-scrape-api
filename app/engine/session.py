"""Per-request browser session lifecycle and filesystem isolation."""

from __future__ import annotations

import errno
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.engine.driver_capabilities import DriverProtocol, call_quietly

if TYPE_CHECKING:
    from app.engine.orchestrator import ScraperEngine
    from app.engine.warm_pool import DriverFingerprint
    from app.engine.work_lease import WorkLease


class ScrapeSession:
    """Encapsulates per-request concurrency registration and filesystem isolation."""

    def __init__(
        self,
        engine: ScraperEngine,
        request_id: str,
        *,
        lease: WorkLease | None = None,
    ) -> None:
        self.engine = engine
        self.request_id = request_id
        self.lease = lease
        self.runtime_dir = engine.runtime_root / request_id
        self.profile_dir = self.runtime_dir / "profile"
        self.driver: DriverProtocol | None = None
        self.adopted_profile_dir: Path | None = None
        self.warm_fingerprint: DriverFingerprint | None = None
        self.warm_hit: bool | None = None
        self._closed = False

    def __enter__(self) -> ScrapeSession:
        self.engine.register_request_id(self.request_id)
        try:
            self.engine.prepare_runtime_for_request()
        except Exception:
            self.engine.unregister_request_id(self.request_id)
            raise
        if self.lease is not None:
            self.lease.register_reclaim(self.force_close)
        return self

    def force_close(self) -> None:
        """Lease-deadline reclaim: close Chromium once so the worker slot can free."""
        if self._closed:
            return
        self._closed = True
        if self.driver is not None:
            call_quietly(self.driver, "close")

    def prepare_runtime_dir(self) -> None:
        """Create the request runtime dir only (warm-path adoption)."""
        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            shutil.rmtree(self.runtime_dir, ignore_errors=True)
            self.engine.prune_runtime_dirs()
            self.runtime_dir.mkdir(parents=True, exist_ok=False)

    def prepare_profile_dirs(self) -> None:
        try:
            self._make_dirs()
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            # Drop any partially created dirs so the retry can recreate them
            # with exist_ok=False after the prune pass frees space.
            shutil.rmtree(self.runtime_dir, ignore_errors=True)
            self.engine.prune_runtime_dirs()
            self._make_dirs()

    def _make_dirs(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=False)
        self.profile_dir.mkdir(parents=True, exist_ok=False)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            self.force_close()
        finally:
            shutil.rmtree(self.runtime_dir, ignore_errors=True)
            if self.adopted_profile_dir is not None:
                shutil.rmtree(self.adopted_profile_dir, ignore_errors=True)
                if self.engine.warm_pool is not None:
                    self.engine.warm_pool.release_adopted(self.adopted_profile_dir)
            self.engine.unregister_request_id(self.request_id)

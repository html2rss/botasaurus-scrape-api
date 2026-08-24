"""Per-request browser session lifecycle and filesystem isolation."""

from __future__ import annotations

import errno
import shutil
from typing import TYPE_CHECKING, Any

from app.engine.driver_capabilities import DriverProtocol, call_quietly

if TYPE_CHECKING:
    from app.engine.orchestrator import ScraperEngine


class ScrapeSession:
    """Encapsulates per-request concurrency registration and filesystem isolation."""

    def __init__(self, engine: ScraperEngine, request_id: str) -> None:
        self.engine = engine
        self.request_id = request_id
        self.runtime_dir = engine.runtime_root / request_id
        self.profile_dir = self.runtime_dir / "profile"
        self.driver: DriverProtocol | None = None

    def __enter__(self) -> ScrapeSession:
        self.engine.register_request_id(self.request_id)
        try:
            self.engine.prepare_runtime_for_request()
        except Exception:
            self.engine.unregister_request_id(self.request_id)
            raise
        return self

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
            if self.driver is not None:
                call_quietly(self.driver, "close")
        finally:
            shutil.rmtree(self.runtime_dir, ignore_errors=True)
            self.engine.unregister_request_id(self.request_id)

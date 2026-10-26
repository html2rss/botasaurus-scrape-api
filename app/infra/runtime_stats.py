"""Periodic runtime heartbeat and startup preflight probes."""

import logging
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.infra.cgroup import read_cgroup_memory
from app.logging_config import get_logger, log_event

logger = get_logger()

CHROMIUM_CANDIDATES = (
    Path(os.environ.get("CHROME_BIN", "")),
    Path("/usr/bin/chromium"),
    Path("/usr/bin/google-chrome"),
)


@dataclass(frozen=True)
class RuntimeSnapshot:
    mem_current: int | None
    mem_max: int | None
    mem_ratio: float | None
    chromium_procs: int
    tmp_free_bytes: int | None
    runtime_dirs: int
    active_requests: int
    warm_present: bool


def count_chromium_procs(*, proc_root: Path = Path("/proc")) -> int:
    """Count processes whose /proc/*/comm is chromium or chrome."""
    if not proc_root.is_dir():
        return 0
    count = 0
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip().lower()
        except OSError:
            continue
        if "chrom" in comm:
            count += 1
    return count


def tmp_free_bytes(*, path: Path = Path("/tmp")) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def home_is_writable(*, home: Path | None = None) -> bool:
    target = home if home is not None else Path.home()
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".scrape_home_writable"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def resolve_chromium_binary() -> Path | None:
    for candidate in CHROMIUM_CANDIDATES:
        if not str(candidate):
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def collect_runtime_snapshot(
    *,
    runtime_root: Path,
    active_requests: int,
    warm_present: bool,
    proc_root: Path = Path("/proc"),
) -> RuntimeSnapshot:
    mem = read_cgroup_memory()
    runtime_dirs = 0
    if runtime_root.is_dir():
        try:
            runtime_dirs = sum(1 for entry in runtime_root.iterdir() if entry.is_dir())
        except OSError:
            runtime_dirs = 0
    return RuntimeSnapshot(
        mem_current=mem.current,
        mem_max=mem.maximum,
        mem_ratio=mem.ratio,
        chromium_procs=count_chromium_procs(proc_root=proc_root),
        tmp_free_bytes=tmp_free_bytes(),
        runtime_dirs=runtime_dirs,
        active_requests=active_requests,
        warm_present=warm_present,
    )


def run_runtime_preflight(
    *,
    runtime_root: Path,
    home: Path | None = None,
) -> bool:
    """Log error (and return False) when HOME, runtime root, or Chromium are unusable."""
    ok = True
    if not home_is_writable(home=home):
        ok = False
        log_event(
            logger,
            logging.ERROR,
            "runtime_preflight",
            check="home_writable",
            ok=False,
            home=str(home or Path.home()),
        )
        _capture_preflight_sentry("home_writable")
    try:
        runtime_root.mkdir(parents=True, exist_ok=True)
        probe = runtime_root / ".preflight_writable"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        ok = False
        log_event(
            logger,
            logging.ERROR,
            "runtime_preflight",
            check="runtime_root_writable",
            ok=False,
            path=str(runtime_root),
            error=str(exc),
        )
        _capture_preflight_sentry("runtime_root_writable")
    chromium = resolve_chromium_binary()
    if chromium is None:
        ok = False
        log_event(
            logger,
            logging.ERROR,
            "runtime_preflight",
            check="chromium_binary",
            ok=False,
        )
        _capture_preflight_sentry("chromium_binary")
    if ok:
        log_event(
            logger,
            logging.INFO,
            "runtime_preflight",
            check="all",
            ok=True,
            chromium=str(chromium),
        )
    return ok


def _capture_preflight_sentry(check: str) -> None:
    from app.infra.sentry import sentry_is_ready

    if not sentry_is_ready():
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_message(
            f"runtime_preflight failed check={check}",
            level="error",
        )
    except Exception:  # fmt: skip
        return


class RuntimeStatsReporter:
    """Daemon heartbeat that emits ``runtime_stats`` on a fixed interval."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        runtime_root: Path,
        active_requests: Callable[[], int],
        warm_present: Callable[[], bool],
    ) -> None:
        self._interval = interval_seconds
        self._runtime_root = runtime_root
        self._active_requests = active_requests
        self._warm_present = warm_present
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._interval <= 0:
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="runtime-stats",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(timeout=self._interval):
            self.emit_once()

    def emit_once(self) -> None:
        snap = collect_runtime_snapshot(
            runtime_root=self._runtime_root,
            active_requests=self._active_requests(),
            warm_present=self._warm_present(),
        )
        log_event(
            logger,
            logging.INFO,
            "runtime_stats",
            mem_current=snap.mem_current,
            mem_max=snap.mem_max,
            mem_ratio=None if snap.mem_ratio is None else round(snap.mem_ratio, 4),
            chromium_procs=snap.chromium_procs,
            tmp_free_bytes=snap.tmp_free_bytes,
            runtime_dirs=snap.runtime_dirs,
            active_requests=snap.active_requests,
            warm_present=snap.warm_present,
        )

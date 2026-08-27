"""Opt-in single-slot prewarmed Chromium driver pool.

Owns spare lifecycle: build, health, TTL, one-shot handoff, refill daemon,
and shutdown. Request-path code only calls ``take`` / ``notify_scrape_finished``.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.engine.driver_capabilities import (
    CdpTabProtocol,
    DriverProtocol,
    call_quietly,
    resolve_cdp_tab,
)
from app.logging_config import get_logger

if TYPE_CHECKING:
    from app.schemas.request import ScrapeRequest

logger = get_logger()

# Set True when Docker Xvfb spike shows concurrent headless=false drivers collide.
PREWARM_HEADLESS_ONLY = False

CGROUP_MEMORY_CURRENT = Path("/sys/fs/cgroup/memory.current")
CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")
MEMORY_PRESSURE_RATIO = 0.70

DriverFactory = Callable[["DriverFingerprint", Path], DriverProtocol]
HealthCheck = Callable[[DriverProtocol], CdpTabProtocol | None]
MemoryPressureCheck = Callable[[], bool]
Clock = Callable[[], float]


def create_driver_from_fingerprint(
    fingerprint: DriverFingerprint, spare_dir: Path
) -> DriverProtocol:
    """Build a Botasaurus Driver for a warm spare (lazy vendor import)."""
    from typing import cast

    from botasaurus.browser import Driver

    window_size = (
        [fingerprint.window_size[0], fingerprint.window_size[1]]
        if fingerprint.window_size is not None
        else None
    )
    return cast(
        DriverProtocol,
        Driver(
            headless=fingerprint.headless,
            enable_xvfb_virtual_display=not fingerprint.headless,
            proxy=fingerprint.proxy,
            profile=str(spare_dir),
            tiny_profile=True,
            block_images=fingerprint.block_images,
            block_images_and_css=fingerprint.block_images_and_css,
            wait_for_complete_page_load=fingerprint.wait_for_complete_page_load,
            user_agent=fingerprint.user_agent,
            window_size=window_size,
            lang=fingerprint.lang,
            remove_default_browser_check_argument=True,
        ),
    )


def cgroup_memory_under_pressure(
    *,
    ratio: float = MEMORY_PRESSURE_RATIO,
    current_path: Path = CGROUP_MEMORY_CURRENT,
    max_path: Path = CGROUP_MEMORY_MAX,
) -> bool:
    """Best-effort cgroup-v2 check; False when unavailable or unlimited."""
    try:
        current = int(current_path.read_text().strip())
        max_raw = max_path.read_text().strip()
        if max_raw == "max":
            return False
        maximum = int(max_raw)
        if maximum <= 0:
            return False
        return (current / maximum) >= ratio
    except (OSError, ValueError):  # fmt: skip
        return False


@dataclass(frozen=True, slots=True)
class DriverFingerprint:
    """Construction-affecting driver options used to gate warm handoff."""

    headless: bool
    proxy: str | None
    block_images: bool
    block_images_and_css: bool
    wait_for_complete_page_load: bool
    user_agent: str | None
    window_size: tuple[int, int] | None
    lang: str | None

    @classmethod
    def from_request(cls, payload: ScrapeRequest) -> DriverFingerprint:
        window: tuple[int, int] | None = None
        if payload.window_size is not None:
            window = (payload.window_size.width, payload.window_size.height)
        return cls(
            headless=payload.headless,
            proxy=payload.proxy,
            block_images=payload.block_images,
            block_images_and_css=payload.block_images_and_css,
            wait_for_complete_page_load=payload.wait_for_complete_page_load,
            user_agent=payload.effective_user_agent,
            window_size=window,
            lang=payload.lang,
        )

    @property
    def fingerprint_hash(self) -> str:
        payload = "|".join(
            [
                f"h={int(self.headless)}",
                f"p={self.proxy or ''}",
                f"bi={int(self.block_images)}",
                f"bic={int(self.block_images_and_css)}",
                f"wpl={int(self.wait_for_complete_page_load)}",
                f"ua={self.user_agent or ''}",
                f"ws={self.window_size!s}",
                f"lang={self.lang or ''}",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return f"DriverFingerprint(hash={self.fingerprint_hash[:12]})"


@dataclass(slots=True)
class _SpareSlot:
    driver: DriverProtocol
    spare_dir: Path
    fingerprint: DriverFingerprint
    created_at: float


class WarmDriverPool:
    """Single-slot warm Chromium spare with dedicated refill daemon thread."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        idle_ttl_seconds: int,
        min_refill_seconds: float,
        driver_factory: DriverFactory,
        health_check: HealthCheck | None = None,
        memory_pressure: MemoryPressureCheck | None = None,
        clock: Clock | None = None,
        headless_only: bool = PREWARM_HEADLESS_ONLY,
        join_timeout_seconds: float = 5.0,
    ) -> None:
        self._runtime_root = runtime_root
        self._idle_ttl_seconds = idle_ttl_seconds
        self._min_refill_seconds = min_refill_seconds
        self._driver_factory = driver_factory
        self._health_check = health_check or resolve_cdp_tab
        self._memory_pressure = memory_pressure or cgroup_memory_under_pressure
        self._clock = clock or time.monotonic
        self._headless_only = headless_only
        self._join_timeout_seconds = join_timeout_seconds

        self._lock = threading.Lock()
        self._spare: _SpareSlot | None = None
        self._desired: DriverFingerprint | None = None
        self._last_refill_at = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._build_count = 0
        self._close_count = 0
        self._building = False

        self._cleanup_orphan_spares()
        self._thread = threading.Thread(
            target=self._refill_loop,
            name="warm-driver-pool-refill",
            daemon=True,
        )
        self._thread.start()

    @property
    def build_count(self) -> int:
        return self._build_count

    @property
    def close_count(self) -> int:
        return self._close_count

    def live_spare_dirs(self) -> set[Path]:
        with self._lock:
            if self._spare is None:
                return set()
            return {self._spare.spare_dir}

    def take(
        self, fingerprint: DriverFingerprint
    ) -> tuple[DriverProtocol, Path] | None:
        if self._headless_only and not fingerprint.headless:
            return None
        with self._lock:
            spare = self._spare
            if spare is None or spare.fingerprint != fingerprint:
                return None
            if self._health_check(spare.driver) is None:
                logger.info(
                    "warm_pool_reap reason=unhealthy fingerprint_hash=%s",
                    spare.fingerprint.fingerprint_hash[:12],
                )
                self._close_spare_unlocked(spare, reason="unhealthy")
                self._spare = None
                return None
            self._spare = None
            age_s = self._clock() - spare.created_at
            logger.info(
                "warm_pool_state present=false age_s=%.1f fingerprint_hash=%s",
                age_s,
                fingerprint.fingerprint_hash[:12],
            )
            return spare.driver, spare.spare_dir

    def notify_scrape_finished(self, fingerprint: DriverFingerprint) -> None:
        if self._stop.is_set():
            return
        if self._headless_only and not fingerprint.headless:
            return
        with self._lock:
            self._desired = fingerprint
        self._wake.set()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=self._join_timeout_seconds)
        # Wall clock for join/wait deadlines so injected test clocks cannot hang.
        deadline = time.monotonic() + self._join_timeout_seconds
        while True:
            with self._lock:
                if not self._building:
                    if self._spare is not None:
                        logger.info(
                            "warm_pool_reap reason=shutdown fingerprint_hash=%s",
                            self._spare.fingerprint.fingerprint_hash[:12],
                        )
                        self._close_spare_unlocked(self._spare, reason="shutdown")
                        self._spare = None
                    self._desired = None
                    return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        with self._lock:
            if self._spare is not None:
                logger.info(
                    "warm_pool_reap reason=shutdown fingerprint_hash=%s",
                    self._spare.fingerprint.fingerprint_hash[:12],
                )
                self._close_spare_unlocked(self._spare, reason="shutdown")
                self._spare = None
            self._desired = None

    def _cleanup_orphan_spares(self) -> None:
        if not self._runtime_root.is_dir():
            return
        for entry in self._runtime_root.iterdir():
            if entry.is_dir() and entry.name.startswith("spare-"):
                shutil.rmtree(entry, ignore_errors=True)
                logger.info("warm_pool_orphan_spare_removed path=%s", entry)

    def _refill_loop(self) -> None:
        while not self._stop.is_set():
            timeout = self._next_wait_seconds()
            self._wake.wait(timeout=timeout)
            self._wake.clear()
            if self._stop.is_set():
                break
            self._maybe_reap_ttl()
            self._maybe_refill()

    def _next_wait_seconds(self) -> float | None:
        with self._lock:
            if self._spare is None or self._idle_ttl_seconds <= 0:
                return None
            remaining = self._idle_ttl_seconds - (
                self._clock() - self._spare.created_at
            )
            return max(0.0, remaining)

    def _maybe_reap_ttl(self) -> None:
        if self._idle_ttl_seconds <= 0:
            return
        with self._lock:
            spare = self._spare
            if spare is None:
                return
            age = self._clock() - spare.created_at
            if age < self._idle_ttl_seconds:
                return
            logger.info(
                "warm_pool_reap reason=ttl fingerprint_hash=%s age_s=%.1f",
                spare.fingerprint.fingerprint_hash[:12],
                age,
            )
            self._close_spare_unlocked(spare, reason="ttl")
            self._spare = None

    def _maybe_refill(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if self._spare is not None or self._building:
                return
            desired = self._desired
            if desired is None:
                return
            if self._headless_only and not desired.headless:
                return
            now = self._clock()
            if (
                self._last_refill_at > 0
                and (now - self._last_refill_at) < self._min_refill_seconds
            ):
                return
            if self._memory_pressure():
                logger.info("warm_pool_refill skipped=memory_pressure")
                return
            self._building = True
            fingerprint = desired

        spare_dir = self._runtime_root / f"spare-{uuid.uuid4()}"
        driver: DriverProtocol | None = None
        try:
            spare_dir.mkdir(parents=True, exist_ok=False)
            driver = self._driver_factory(fingerprint, spare_dir)
            with self._lock:
                self._build_count += 1
                self._last_refill_at = self._clock()
                if self._stop.is_set() or self._spare is not None:
                    self._close_driver(driver)
                    shutil.rmtree(spare_dir, ignore_errors=True)
                    return
                self._spare = _SpareSlot(
                    driver=driver,
                    spare_dir=spare_dir,
                    fingerprint=fingerprint,
                    created_at=self._clock(),
                )
                driver = None
                logger.info(
                    "warm_pool_refill fingerprint_hash=%s present=true",
                    fingerprint.fingerprint_hash[:12],
                )
                logger.info(
                    "warm_pool_state present=true age_s=0.0 fingerprint_hash=%s",
                    fingerprint.fingerprint_hash[:12],
                )
        except Exception as exc:
            logger.warning(
                "warm_pool_refill_failed fingerprint_hash=%s error=%s",
                fingerprint.fingerprint_hash[:12],
                exc,
            )
            if driver is not None:
                self._close_driver(driver)
            shutil.rmtree(spare_dir, ignore_errors=True)
        finally:
            with self._lock:
                self._building = False

    def _close_spare_unlocked(self, spare: _SpareSlot, *, reason: str) -> None:
        del reason
        self._close_driver(spare.driver)
        shutil.rmtree(spare.spare_dir, ignore_errors=True)

    def _close_driver(self, driver: DriverProtocol) -> None:
        call_quietly(driver, "close")
        self._close_count += 1

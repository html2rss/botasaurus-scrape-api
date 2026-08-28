"""Opt-in single-slot prewarmed Chromium driver pool.

Owns spare lifecycle: build, health, TTL, one-shot handoff, refill daemon,
and shutdown. Request-path code only calls ``take`` / ``release_adopted`` /
``notify_scrape_finished``.

Spares are constructed idle at refill time; per-request cookies, headers,
and tracker blocking are applied in ``configure_driver()`` after adoption,
not during spare build.
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
# Floor for deferred refill wake when min_refill is 0 (memory-pressure skip).
MEMORY_PRESSURE_RETRY_SECONDS = 1.0

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
        self._next_refill_at = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._build_count = 0
        self._close_count = 0
        self._building = False
        self._building_dir: Path | None = None
        self._adopted_dirs: set[Path] = set()

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

    def ready_spare_dirs(self) -> set[Path]:
        """Dirs for the ready (takeable) slot only."""
        with self._lock:
            if self._spare is None:
                return set()
            return {self._spare.spare_dir}

    def live_spare_dirs(self) -> set[Path]:
        """Dirs that must not be pruned: ready, in-build, or adopted-in-use."""
        with self._lock:
            dirs = set(self._adopted_dirs)
            if self._spare is not None:
                dirs.add(self._spare.spare_dir)
            if self._building_dir is not None:
                dirs.add(self._building_dir)
            return dirs

    def take(
        self, fingerprint: DriverFingerprint
    ) -> tuple[DriverProtocol, Path] | None:
        if self._headless_only and not fingerprint.headless:
            return None
        with self._lock:
            spare = self._spare
            if spare is None or spare.fingerprint != fingerprint:
                return None
            # Claim the slot before health probe so concurrent take cannot race,
            # and protect the dir from prune while CDP runs outside the lock.
            self._spare = None
            self._adopted_dirs.add(spare.spare_dir)

        if (
            self._probe_health(
                spare.driver,
                fingerprint_hash=spare.fingerprint.fingerprint_hash[:12],
            )
            is None
        ):
            with self._lock:
                self._abort_adopted_take(spare, reason="unhealthy")
            return None

        age_s = self._clock() - spare.created_at
        logger.info(
            "warm_pool_state present=false age_s=%.1f fingerprint_hash=%s",
            age_s,
            fingerprint.fingerprint_hash[:12],
        )
        return spare.driver, spare.spare_dir

    def release_adopted(self, spare_dir: Path) -> None:
        """Drop prune protection after the adopting session cleaned up the dir."""
        with self._lock:
            self._adopted_dirs.discard(spare_dir)

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
                    self._next_refill_at = 0.0
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
            self._next_refill_at = 0.0

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
            waits: list[float] = []
            now = self._clock()
            if self._spare is not None and self._idle_ttl_seconds > 0:
                remaining = self._idle_ttl_seconds - (now - self._spare.created_at)
                waits.append(max(0.0, remaining))
            if (
                self._next_refill_at > 0
                and self._desired is not None
                and self._spare is None
                and not self._building
            ):
                waits.append(max(0.0, self._next_refill_at - now))
            if not waits:
                return None
            return min(waits)

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

    def _schedule_deferred_refill(self, not_before: float) -> None:
        self._next_refill_at = not_before

    def _maybe_reap_stale_spare(self, desired: DriverFingerprint) -> None:
        """Requires ``_lock`` held. Drop ready spare when it no longer matches desired."""
        spare = self._spare
        if spare is None or spare.fingerprint == desired:
            return
        logger.info(
            "warm_pool_reap reason=stale_fingerprint fingerprint_hash=%s",
            spare.fingerprint.fingerprint_hash[:12],
        )
        self._close_spare_unlocked(spare, reason="stale_fingerprint")
        self._spare = None

    def _maybe_refill(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if self._building:
                return
            desired = self._desired
            if desired is None:
                return
            self._maybe_reap_stale_spare(desired)
            if self._spare is not None:
                return
            if self._headless_only and not desired.headless:
                return
            now = self._clock()
            if (
                self._last_refill_at > 0
                and (now - self._last_refill_at) < self._min_refill_seconds
            ):
                self._schedule_deferred_refill(
                    self._last_refill_at + self._min_refill_seconds
                )
                return
            if self._memory_pressure():
                logger.info("warm_pool_refill skipped=memory_pressure")
                delay = max(self._min_refill_seconds, MEMORY_PRESSURE_RETRY_SECONDS)
                self._schedule_deferred_refill(now + delay)
                return
            self._building = True
            self._next_refill_at = 0.0
            fingerprint = desired
            spare_dir = self._runtime_root / f"spare-{uuid.uuid4()}"
            self._building_dir = spare_dir

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
                self._building_dir = None

    def _probe_health(
        self, driver: DriverProtocol, *, fingerprint_hash: str
    ) -> CdpTabProtocol | None:
        try:
            return self._health_check(driver)
        except Exception as exc:
            logger.warning(
                "warm_pool_health_probe_failed fingerprint_hash=%s error=%s",
                fingerprint_hash,
                type(exc).__name__,
            )
            return None

    def _abort_adopted_take(self, spare: _SpareSlot, *, reason: str) -> None:
        """Requires ``_lock`` held. Single path for failed health probes."""
        self._adopted_dirs.discard(spare.spare_dir)
        logger.info(
            "warm_pool_reap reason=%s fingerprint_hash=%s",
            reason,
            spare.fingerprint.fingerprint_hash[:12],
        )
        self._close_spare_unlocked(spare, reason=reason)

    def _close_spare_unlocked(self, spare: _SpareSlot, *, reason: str) -> None:
        del reason
        self._close_driver(spare.driver)
        shutil.rmtree(spare.spare_dir, ignore_errors=True)

    def _close_driver(self, driver: DriverProtocol) -> None:
        call_quietly(driver, "close")
        self._close_count += 1

"""Best-effort cgroup-v2 memory probes shared by warm pool and runtime stats."""

from dataclasses import dataclass
from pathlib import Path

CGROUP_MEMORY_CURRENT = Path("/sys/fs/cgroup/memory.current")
CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")
MEMORY_PRESSURE_RATIO = 0.70


@dataclass(frozen=True)
class CgroupMemorySnapshot:
    current: int | None
    maximum: int | None

    @property
    def ratio(self) -> float | None:
        if self.current is None or self.maximum is None or self.maximum <= 0:
            return None
        return self.current / self.maximum

    def under_pressure(self, *, ratio: float = MEMORY_PRESSURE_RATIO) -> bool:
        mem_ratio = self.ratio
        if mem_ratio is None:
            return False
        return mem_ratio >= ratio


def read_cgroup_memory(
    *,
    current_path: Path = CGROUP_MEMORY_CURRENT,
    max_path: Path = CGROUP_MEMORY_MAX,
) -> CgroupMemorySnapshot:
    """Return current/max bytes; fields are None when unavailable or unlimited."""
    try:
        current = int(current_path.read_text().strip())
        max_raw = max_path.read_text().strip()
        if max_raw == "max":
            return CgroupMemorySnapshot(current=current, maximum=None)
        maximum = int(max_raw)
        if maximum <= 0:
            return CgroupMemorySnapshot(current=current, maximum=None)
        return CgroupMemorySnapshot(current=current, maximum=maximum)
    except (OSError, ValueError):  # fmt: skip
        return CgroupMemorySnapshot(current=None, maximum=None)


def cgroup_memory_under_pressure(
    *,
    ratio: float = MEMORY_PRESSURE_RATIO,
    current_path: Path = CGROUP_MEMORY_CURRENT,
    max_path: Path = CGROUP_MEMORY_MAX,
) -> bool:
    """Best-effort cgroup-v2 check; False when unavailable or unlimited."""
    return read_cgroup_memory(
        current_path=current_path, max_path=max_path
    ).under_pressure(ratio=ratio)

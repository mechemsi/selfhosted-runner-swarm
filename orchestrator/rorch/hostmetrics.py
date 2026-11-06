# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""What this host has free: cores, load, memory and CPU temperature.

Read from /proc and /sys inside the orchestrator container. In an LXC without lxcfs,
load and memory are the physical host's, which is what peers need to compare.
"""

import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path

# hwmon chip name -> the label of its package/die sensor. First match wins.
CPU_SENSORS = (("k10temp", "Tctl"), ("zenpower", "Tdie"), ("coretemp", "Package id 0"))


@dataclass(frozen=True)
class HostMetrics:
    cores: int  # CPUs this process may run on (cgroup/affinity limit)
    cpu_count: int  # CPUs the kernel sees; load average is relative to these
    load1: float
    mem_total_mb: int
    mem_available_mb: int
    temp_c: float | None  # None: no sensor readable, shown as "unknown"

    @property
    def free_cores(self) -> int:
        busy_share = self.load1 / self.cpu_count if self.cpu_count else 1.0
        return math.floor(self.cores * max(0.0, 1.0 - busy_share))

    def is_hot(self, max_temp_c: float) -> bool:
        """Unknown temperature never blocks: a host without sensors must still run jobs."""
        return self.temp_c is not None and self.temp_c >= max_temp_c

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "free_cores": self.free_cores}


def read(root: str = "/") -> HostMetrics:
    base = Path(root)
    return HostMetrics(
        cores=_cores(),
        cpu_count=os.cpu_count() or 1,
        load1=_load1(base / "proc/loadavg"),
        mem_total_mb=_meminfo_mb(base / "proc/meminfo", "MemTotal"),
        mem_available_mb=_meminfo_mb(base / "proc/meminfo", "MemAvailable"),
        temp_c=cpu_temp(base / "sys/class/hwmon"),
    )


def _cores() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _load1(path: Path) -> float:
    try:
        return float(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _meminfo_mb(path: Path, key: str) -> int:
    try:
        for line in path.read_text().splitlines():
            name, _, rest = line.partition(":")
            if name == key:
                return int(rest.split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def cpu_temp(hwmon: Path) -> float | None:
    """CPU package temperature in C, or None when no known sensor is readable."""
    chips = {}
    for chip in sorted(hwmon.glob("hwmon*")):
        name = _text(chip / "name")
        if name:
            chips.setdefault(name, chip)
    for name, label in CPU_SENSORS:
        chip = chips.get(name)
        if chip is None:
            continue
        for label_file in sorted(chip.glob("temp*_label")):
            if _text(label_file) == label:
                value = _text(label_file.with_name(label_file.name.replace("_label", "_input")))
                if value and value.lstrip("-").isdigit():
                    return int(value) / 1000
    return None


def _text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""

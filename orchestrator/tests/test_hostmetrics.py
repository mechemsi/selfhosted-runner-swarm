# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from pathlib import Path

from rorch import hostmetrics
from rorch.hostmetrics import HostMetrics


def _chip(hwmon: Path, index: int, name: str, sensors: dict[str, int]) -> None:
    chip = hwmon / f"hwmon{index}"
    chip.mkdir(parents=True)
    (chip / "name").write_text(f"{name}\n")
    for n, (label, millic) in enumerate(sensors.items(), start=1):
        (chip / f"temp{n}_label").write_text(f"{label}\n")
        (chip / f"temp{n}_input").write_text(f"{millic}\n")


def test_reads_k10temp_tctl_and_ignores_other_chips(tmp_path: Path) -> None:
    hwmon = tmp_path / "sys/class/hwmon"
    _chip(hwmon, 0, "nvme", {"Composite": 39850})
    _chip(hwmon, 1, "amdgpu", {"edge": 51000})
    _chip(hwmon, 2, "k10temp", {"Tccd1": 70000, "Tctl": 72625})
    assert hostmetrics.cpu_temp(hwmon) == 72.625


def test_reads_intel_coretemp_package(tmp_path: Path) -> None:
    hwmon = tmp_path / "hwmon"
    _chip(hwmon, 0, "coretemp", {"Core 0": 50000, "Package id 0": 55000})
    assert hostmetrics.cpu_temp(hwmon) == 55.0


def test_absent_or_unreadable_sensor_is_unknown_not_cool(tmp_path: Path) -> None:
    hwmon = tmp_path / "hwmon"
    assert hostmetrics.cpu_temp(hwmon) is None
    _chip(hwmon, 0, "nvme", {"Composite": 39850})
    _chip(hwmon, 1, "k10temp", {"Tctl": 0})
    (hwmon / "hwmon1/temp1_input").write_text("garbage\n")
    assert hostmetrics.cpu_temp(hwmon) is None
    unknown = HostMetrics(4, 4, 0.0, 1, 1, None)
    assert not unknown.is_hot(85)


def test_read_parses_proc_files(tmp_path: Path) -> None:
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/loadavg").write_text("3.50 2.00 1.00 2/300 1234\n")
    (tmp_path / "proc/meminfo").write_text(
        "MemTotal:       32768000 kB\nMemFree: 1 kB\nMemAvailable:   16384000 kB\n"
    )
    metrics = hostmetrics.read(str(tmp_path))
    assert metrics.load1 == 3.5
    assert (metrics.mem_total_mb, metrics.mem_available_mb) == (32000, 16000)
    assert metrics.temp_c is None


def test_free_cores_and_hot() -> None:
    m = HostMetrics(
        cores=10, cpu_count=12, load1=6.0, mem_total_mb=1, mem_available_mb=1, temp_c=85.0
    )
    assert m.free_cores == 5
    assert m.is_hot(85) and not m.is_hot(86)
    overloaded = HostMetrics(6, 16, 40.0, 1, 1, 50.0)
    assert overloaded.free_cores == 0
    assert overloaded.as_dict()["free_cores"] == 0

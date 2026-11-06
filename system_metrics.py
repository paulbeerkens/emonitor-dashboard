"""Lightweight Raspberry Pi health readings exposed through Linux sysfs."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


class SystemMetrics:
  """Read CPU, memory, disk, temperature, and fan health without Pi tools."""

  def __init__(
    self,
    sysfs_root: str | Path = "/sys",
    proc_root: str | Path = "/proc",
    disk_path: str | Path = "/",
  ):
    self.sysfs_root = Path(sysfs_root)
    self.proc_root = Path(proc_root)
    self.disk_path = Path(disk_path)
    self._previous_cpu_times = self._cpu_times()

  @staticmethod
  def _read_number(path: Path) -> float | None:
    try:
      return float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
      return None

  def _cpu_temperature(self) -> float | None:
    thermal_root = self.sysfs_root / "class" / "thermal"
    zones = list(thermal_root.glob("thermal_zone*"))
    preferred: list[Path] = []
    fallback: list[Path] = []
    for zone in zones:
      try:
        zone_type = (zone / "type").read_text(encoding="utf-8").strip().casefold()
      except OSError:
        zone_type = ""
      destination = preferred if any(
        name in zone_type for name in ("cpu", "soc", "bcm")
      ) else fallback
      destination.append(zone / "temp")

    for path in preferred + fallback:
      value = self._read_number(path)
      if value is not None:
        return round(value / 1000 if value > 1000 else value, 1)
    return None

  def _fan_speed(self) -> int | None:
    patterns = (
      "class/hwmon/hwmon*/fan*_input",
      "devices/platform/cooling_fan/hwmon/hwmon*/fan*_input",
    )
    for pattern in patterns:
      for path in self.sysfs_root.glob(pattern):
        value = self._read_number(path)
        if value is not None:
          return round(value)
    return None

  def _cpu_times(self) -> tuple[float, float] | None:
    try:
      fields = (self.proc_root / "stat").read_text(
        encoding="utf-8"
      ).splitlines()[0].split()
      if not fields or fields[0] != "cpu":
        return None
      values = [float(value) for value in fields[1:]]
    except (OSError, ValueError, IndexError):
      return None
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total, idle

  def _cpu_utilization(self) -> float | None:
    current = self._cpu_times()
    previous = self._previous_cpu_times
    self._previous_cpu_times = current
    if current is None or previous is None:
      return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
      return None
    return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)

  def _memory(self) -> dict[str, int | float | None]:
    try:
      values = {}
      for line in (self.proc_root / "meminfo").read_text(
        encoding="utf-8"
      ).splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip().split()[0]) * 1024
      total = values["MemTotal"]
      available = values["MemAvailable"]
      used = max(0, total - available)
      return {
        "memory_used_bytes": used,
        "memory_total_bytes": total,
        "memory_used_percent": round(used / total * 100, 1),
      }
    except (OSError, ValueError, KeyError):
      return {
        "memory_used_bytes": None,
        "memory_total_bytes": None,
        "memory_used_percent": None,
      }

  def snapshot(self) -> dict[str, Any]:
    try:
      disk = shutil.disk_usage(self.disk_path)
      disk_values: dict[str, int | float | None] = {
        "disk_free_bytes": disk.free,
        "disk_total_bytes": disk.total,
        "disk_free_percent": round(disk.free / disk.total * 100, 1),
      }
    except OSError:
      disk_values = {
        "disk_free_bytes": None,
        "disk_total_bytes": None,
        "disk_free_percent": None,
      }

    return {
      "cpu_temperature_c": self._cpu_temperature(),
      "cpu_utilization_percent": self._cpu_utilization(),
      "fan_speed_rpm": self._fan_speed(),
      **self._memory(),
      **disk_values,
    }


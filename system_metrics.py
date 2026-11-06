"""Lightweight Raspberry Pi health readings exposed through Linux sysfs."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


class SystemMetrics:
  """Read CPU temperature, root disk space, and fan speed without Pi tools."""

  def __init__(self, sysfs_root: str | Path = "/sys", disk_path: str | Path = "/"):
    self.sysfs_root = Path(sysfs_root)
    self.disk_path = Path(disk_path)

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
      "fan_speed_rpm": self._fan_speed(),
      **disk_values,
    }


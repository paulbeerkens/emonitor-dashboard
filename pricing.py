"""Cached access to ComEd's public five-minute real-time price feed."""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


COMED_API_URL = "https://hourlypricing.comed.com/api"
CHICAGO = ZoneInfo("America/Chicago")


class ComEdPricingService:
  """Fetch at most once every five minutes and keep the latest good response."""

  def __init__(self, cache_seconds: int = 300, timeout_seconds: int = 10):
    self.cache_seconds = cache_seconds
    self.timeout_seconds = timeout_seconds
    self._lock = threading.Lock()
    self._last_attempt: datetime | None = None
    self._cached: dict[str, Any] | None = None

  def snapshot(self, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(timezone.utc)
    with self._lock:
      if (
        self._last_attempt is not None
        and (now - self._last_attempt).total_seconds() < self.cache_seconds
      ):
        return self._cached
      self._last_attempt = now
      try:
        self._cached = self._download(now)
      except (OSError, ValueError, json.JSONDecodeError):
        # The dashboard can fall back to the prices already stored by the collector.
        pass
      return self._cached

  def _download(self, now: datetime) -> dict[str, Any]:
    local_now = now.astimezone(CHICAGO)
    local_start = local_now - timedelta(hours=25)
    query = urllib.parse.urlencode({
      "type": "5minutefeed",
      "datestart": local_start.strftime("%Y%m%d%H%M"),
      "dateend": local_now.strftime("%Y%m%d%H%M"),
      "format": "json",
    })
    with urllib.request.urlopen(
      f"{COMED_API_URL}?{query}", timeout=self.timeout_seconds
    ) as response:
      payload = json.load(response)

    samples = sorted(
      (
        int(item["millisUTC"]),
        float(item["price"]),
      )
      for item in payload
    )
    if not samples:
      raise ValueError("ComEd returned no prices")

    hourly: dict[int, list[float]] = defaultdict(list)
    for timestamp_ms, price in samples:
      sampled_at = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
      hour_start_ms = round(
        sampled_at.replace(minute=0, second=0, microsecond=0).timestamp() * 1000
      )
      hourly[hour_start_ms].append(price)

    return {
      "current_price_cents_per_kwh": samples[-1][1],
      "price_at_ms": samples[-1][0],
      "hourly_prices": [
        {
          "hour_start_ms": hour_start_ms,
          "price_cents_per_kwh": sum(prices) / len(prices),
        }
        for hour_start_ms, prices in sorted(hourly.items())
      ],
      "source": "ComEd five-minute feed",
    }


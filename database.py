"""Read-only access to the eMonitor collector's SQLite database."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SUMMARY_NAMES = {"main power", "solar panels"}
CHICAGO = ZoneInfo("America/Chicago")
CHICAGO_LATITUDE = 41.8781
CHICAGO_LONGITUDE = -87.6298


class DashboardDatabase:
  """Query collector data without modifying or blocking its database."""

  def __init__(
    self,
    database_path: str | Path,
    *,
    latitude: float = CHICAGO_LATITUDE,
    longitude: float = CHICAGO_LONGITUDE,
  ):
    self.database_path = Path(database_path).expanduser().resolve()
    self.latitude = latitude
    self.longitude = longitude

  def _connect(self) -> sqlite3.Connection:
    if not self.database_path.is_file():
      raise FileNotFoundError(f"eMonitor database not found: {self.database_path}")
    connection = sqlite3.connect(
      f"file:{self.database_path}?mode=ro", uri=True, timeout=5
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection

  @staticmethod
  def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
      "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
      (table_name,),
    ).fetchone() is not None

  @staticmethod
  def _metadata(connection: sqlite3.Connection) -> dict[int, str]:
    if not DashboardDatabase._table_exists(connection, "meta_data"):
      return {}
    return {
      row["id"]: row["name"]
      for row in connection.execute("SELECT id, name FROM meta_data")
    }

  @staticmethod
  def _checkpoint_readings(connection: sqlite3.Connection) -> tuple[int | None, list[dict[str, Any]]]:
    if not DashboardDatabase._table_exists(connection, "collector_state"):
      return None, []
    row = connection.execute(
      "SELECT state_json FROM collector_state WHERE id=1"
    ).fetchone()
    if not row:
      return None, []
    state = json.loads(row["state_json"])
    timestamp = state.get("last_sample_time")
    sampled_at_ms = None
    if timestamp:
      sampled_at_ms = round(datetime.fromisoformat(timestamp).timestamp() * 1000)
    return sampled_at_ms, state.get("last_readings", [])

  @staticmethod
  def _hour_start_ms(value: datetime) -> int:
    return round(
      value.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
      ).timestamp() * 1000
    )

  @staticmethod
  def _half_hour_start_ms(value: datetime) -> int:
    utc_value = value.astimezone(timezone.utc)
    return round(
      utc_value.replace(
        minute=0 if utc_value.minute < 30 else 30,
        second=0,
        microsecond=0,
      ).timestamp() * 1000
    )

  def _stored_market_prices(
    self,
    connection: sqlite3.Connection,
  ) -> tuple[dict[int, float], dict[int, float], tuple[int, float] | None]:
    """Load legacy hourly prices, preferred half-hours, and latest sample."""
    hourly_prices: dict[int, float] = {}
    half_hour_prices: dict[int, float] = {}
    latest: tuple[int, float] | None = None
    if self._table_exists(connection, "comed_hourly_prices"):
      for row in connection.execute(
        "SELECT hour_start, price_cents_per_kwh "
        "FROM comed_hourly_prices ORDER BY hour_start"
      ):
        timestamp_ms = self._hour_start_ms(datetime.fromisoformat(row["hour_start"]))
        price = float(row["price_cents_per_kwh"])
        hourly_prices[timestamp_ms] = price
        latest = (timestamp_ms, price)
    if self._table_exists(connection, "comed_half_hour_prices"):
      for row in connection.execute(
        "SELECT period_start, average_price_cents_per_kwh "
        "FROM comed_half_hour_prices ORDER BY period_start"
      ):
        timestamp_ms = self._half_hour_start_ms(
          datetime.fromisoformat(row["period_start"])
        )
        half_hour_prices[timestamp_ms] = float(
          row["average_price_cents_per_kwh"]
        )
    if self._table_exists(connection, "comed_price_samples"):
      row = connection.execute(
        "SELECT sampled_at_ms, price_cents_per_kwh FROM comed_price_samples "
        "ORDER BY sampled_at_ms DESC LIMIT 1"
      ).fetchone()
      if row:
        latest = (int(row["sampled_at_ms"]), float(row["price_cents_per_kwh"]))
    return hourly_prices, half_hour_prices, latest

  def _market_price(
    self,
    value: datetime,
    hourly_prices: dict[int, float],
    half_hour_prices: dict[int, float],
  ) -> float | None:
    return half_hour_prices.get(
      self._half_hour_start_ms(value),
      hourly_prices.get(self._hour_start_ms(value)),
    )

  def _price_history(
    self,
    now: datetime,
    hours: int,
    hourly_prices: dict[int, float],
    half_hour_prices: dict[int, float],
  ) -> list[dict[str, Any]]:
    current_start_ms = self._half_hour_start_ms(now)
    first_start_ms = current_start_ms - hours * 60 * 60 * 1000
    return [
      {
        "period_start_ms": period_start_ms,
        "price_cents_per_kwh": self._market_price(
          datetime.fromtimestamp(period_start_ms / 1000, timezone.utc),
          hourly_prices,
          half_hour_prices,
        ),
        "current": period_start_ms == current_start_ms,
      }
      for period_start_ms in range(
        first_start_ms, current_start_ms + 1, 30 * 60 * 1000
      )
    ]

  def _five_day_price_history(
    self,
    now: datetime,
    hourly_prices: dict[int, float],
    half_hour_prices: dict[int, float],
  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return five local calendar days plus their true daylight windows."""
    local_now = now.astimezone(CHICAGO)
    first_date = local_now.date() - timedelta(days=4)
    final_date = local_now.date() + timedelta(days=1)
    start = datetime.combine(first_date, datetime.min.time(), CHICAGO)
    end = datetime.combine(final_date, datetime.min.time(), CHICAGO)
    start_ms = round(start.astimezone(timezone.utc).timestamp() * 1000)
    end_ms = round(end.astimezone(timezone.utc).timestamp() * 1000)
    current_start_ms = self._half_hour_start_ms(now)
    rows = [
      {
        "period_start_ms": period_start_ms,
        "price_cents_per_kwh": (
          self._market_price(
            datetime.fromtimestamp(period_start_ms / 1000, timezone.utc),
            hourly_prices,
            half_hour_prices,
          )
          if period_start_ms <= current_start_ms else None
        ),
        "current": period_start_ms == current_start_ms,
      }
      for period_start_ms in range(start_ms, end_ms, 30 * 60 * 1000)
    ]
    days = []
    for offset in range(5):
      local_date = first_date + timedelta(days=offset)
      day_start = datetime.combine(local_date, datetime.min.time(), CHICAGO)
      day_end = datetime.combine(
        local_date + timedelta(days=1), datetime.min.time(), CHICAGO
      )
      sunrise, sunset, _, _ = self._solar_window(local_date)
      days.append({
        "date": local_date.isoformat(),
        "day_start_ms": round(day_start.timestamp() * 1000),
        "day_end_ms": round(day_end.timestamp() * 1000),
        "sunrise_ms": round(sunrise.timestamp() * 1000) if sunrise else None,
        "sunset_ms": round(sunset.timestamp() * 1000) if sunset else None,
      })
    return rows, days

  def _cost_summary(
    self,
    connection: sqlite3.Connection,
    metadata: dict[int, str],
    now: datetime,
    current_main_power_watts: int | None,
    variable_addon_cents_per_kwh: float,
  ) -> tuple[dict[str, Any], dict[str, Any]]:
    hourly_prices, half_hour_prices, stored_latest = self._stored_market_prices(
      connection
    )
    price_at_ms, current_market_price = stored_latest or (None, None)
    price_age_seconds = (
      max(0, round(now.timestamp() - price_at_ms / 1000))
      if price_at_ms is not None else None
    )
    if price_age_seconds is None:
      price_freshness = "unavailable"
    elif price_age_seconds <= 10 * 60:
      price_freshness = "current"
    elif price_age_seconds <= 20 * 60:
      price_freshness = "delayed"
    else:
      price_freshness = "stale"

    effective_rate = (
      current_market_price + variable_addon_cents_per_kwh
      if current_market_price is not None else None
    )
    current_cost_per_hour = (
      current_main_power_watts / 1000 * effective_rate / 100
      if current_main_power_watts is not None and effective_rate is not None
      else None
    )
    five_day_prices, five_day_daylight = self._five_day_price_history(
      now, hourly_prices, half_hour_prices
    )
    pricing = {
      "market_price_cents_per_kwh": current_market_price,
      "variable_addon_cents_per_kwh": variable_addon_cents_per_kwh,
      "effective_variable_rate_cents_per_kwh": effective_rate,
      "current_cost_dollars_per_hour": current_cost_per_hour,
      "price_at_ms": price_at_ms,
      "price_age_seconds": price_age_seconds,
      "price_freshness": price_freshness,
      "source": "Collector SQLite price tables",
      "price_history_24h": self._price_history(
        now, 24, hourly_prices, half_hour_prices
      ),
      "price_history_5d": five_day_prices,
      "price_daylight_windows_5d": five_day_daylight,
    }

    cutoff = now - timedelta(hours=24)
    circuit_totals: dict[int, dict[str, Any]] = {}
    home_energy_kwh = 0.0
    home_priced_energy_kwh = 0.0
    home_cost = 0.0
    burn_rate_history: list[dict[str, Any]] = []
    if self._table_exists(connection, "channel_usage"):
      rows = connection.execute(
        "SELECT id, period_start, period_end, energy_wh, average_power_watts "
        "FROM channel_usage "
        "WHERE period_end>? ORDER BY period_start, id",
        (cutoff.isoformat(),),
      )
      for row in rows:
        channel_id = row["id"]
        name = metadata.get(channel_id, f"Channel {channel_id}")
        energy_kwh = float(row["energy_wh"]) / 1000
        period_start = datetime.fromisoformat(row["period_start"])
        market_price = self._market_price(
          period_start, hourly_prices, half_hour_prices
        )
        cost = (
          energy_kwh * (market_price + variable_addon_cents_per_kwh) / 100
          if market_price is not None else None
        )
        if name.casefold() == "main power":
          home_energy_kwh += energy_kwh
          if cost is not None:
            home_priced_energy_kwh += energy_kwh
            home_cost += cost
            burn_rate_history.append({
              "period_start_ms": round(period_start.timestamp() * 1000),
              "period_end_ms": round(
                datetime.fromisoformat(row["period_end"]).timestamp() * 1000
              ),
              "dollars_per_hour": (
                float(row["average_power_watts"]) / 1000
                * (market_price + variable_addon_cents_per_kwh) / 100
              ),
              "average_power_watts": float(row["average_power_watts"]),
              "current": False,
            })
          continue
        if (
          name.casefold() in SUMMARY_NAMES
          or name.casefold().startswith("channel ")
        ):
          continue
        total = circuit_totals.setdefault(channel_id, {
          "channel_id": channel_id,
          "channel_name": name,
          "energy_kwh": 0.0,
          "priced_energy_kwh": 0.0,
          "cost_dollars": 0.0,
        })
        total["energy_kwh"] += energy_kwh
        if cost is not None:
          total["priced_energy_kwh"] += energy_kwh
          total["cost_dollars"] += cost

    all_circuits = sorted(
      circuit_totals.values(), key=lambda row: row["energy_kwh"], reverse=True
    )
    for row in all_circuits:
      if row["priced_energy_kwh"] == 0:
        row["cost_dollars"] = None
      row["energy_kwh"] = round(row["energy_kwh"], 4)
      row["priced_energy_percent"] = round(
        row["priced_energy_kwh"] / row["energy_kwh"] * 100, 1
      ) if row["energy_kwh"] else 0
      row.pop("priced_energy_kwh")
    circuits = all_circuits[:20]

    # Add the open half-hour using the accumulator's integrated energy so far.
    current_average_power = None
    current_period_start = now.replace(
      minute=0 if now.minute < 30 else 30, second=0, microsecond=0
    )
    if self._table_exists(connection, "collector_state"):
      state_row = connection.execute(
        "SELECT state_json FROM collector_state WHERE id=1"
      ).fetchone()
      if state_row:
        state = json.loads(state_row["state_json"])
        state_start = state.get("period_start")
        main_ids = {
          channel_id for channel_id, name in metadata.items()
          if name.casefold() == "main power"
        }
        measured = state.get("measured_seconds", {})
        energy = state.get("energy_watt_seconds", {})
        total_measured = sum(float(measured.get(str(item), 0)) for item in main_ids)
        total_energy = sum(float(energy.get(str(item), 0)) for item in main_ids)
        if state_start:
          current_period_start = datetime.fromisoformat(state_start)
        if total_measured > 0:
          current_average_power = total_energy / total_measured
    if current_average_power is None and current_main_power_watts is not None:
      current_average_power = float(current_main_power_watts)
    if current_average_power is not None and effective_rate is not None:
      burn_rate_history.append({
        "period_start_ms": round(current_period_start.timestamp() * 1000),
        "period_end_ms": round(now.timestamp() * 1000),
        "dollars_per_hour": current_average_power / 1000 * effective_rate / 100,
        "average_power_watts": current_average_power,
        "current": True,
      })
    burn_rate_history.sort(key=lambda row: row["period_start_ms"])
    pricing["burn_rate_history"] = burn_rate_history[-49:]

    usage = {
      "energy_kwh": round(home_energy_kwh, 4),
      "cost_dollars": home_cost if home_priced_energy_kwh else None,
      "priced_energy_percent": round(
        home_priced_energy_kwh / home_energy_kwh * 100, 1
      ) if home_energy_kwh else 0,
      "circuits": circuits,
      "circuit_breakdown": all_circuits,
    }
    return pricing, usage

  @staticmethod
  def _usage_period(period: str, now: datetime) -> tuple[datetime, datetime, str]:
    rolling_periods = {
      "24h": (timedelta(hours=24), "Last 24 hours"),
      "7d": (timedelta(days=7), "Last 7 days"),
      "30d": (timedelta(days=30), "Last 30 days"),
      "365d": (timedelta(days=365), "Last 365 days"),
    }
    if period in rolling_periods:
      duration, label = rolling_periods[period]
      return now - duration, now, label
    if not re.fullmatch(r"\d{4}-\d{2}", period):
      raise ValueError("Unknown circuit-usage period")
    year, month = (int(value) for value in period.split("-"))
    if not 1 <= month <= 12:
      raise ValueError("Unknown circuit-usage period")
    local_start = datetime(year, month, 1, tzinfo=CHICAGO)
    local_end = (
      datetime(year + 1, 1, 1, tzinfo=CHICAGO)
      if month == 12 else datetime(year, month + 1, 1, tzinfo=CHICAGO)
    )
    return (
      local_start.astimezone(timezone.utc),
      local_end.astimezone(timezone.utc),
      local_start.strftime("%B %Y"),
    )

  @staticmethod
  def _available_months(
    connection: sqlite3.Connection,
  ) -> list[dict[str, str]]:
    rows = connection.execute(
      "SELECT DISTINCT substr(period_start, 1, 7) AS month "
      "FROM channel_usage WHERE period_start IS NOT NULL ORDER BY month DESC"
    )
    months = []
    for row in rows:
      value = row["month"]
      if value and re.fullmatch(r"\d{4}-\d{2}", value):
        parsed = datetime.strptime(value, "%Y-%m")
        months.append({"value": value, "label": parsed.strftime("%B %Y")})
    return months

  def circuit_usage(
    self,
    period: str = "24h",
    *,
    variable_addon_cents_per_kwh: float = 7.791438,
  ) -> dict[str, Any]:
    """Return the 20 highest-energy branch circuits for a selected period."""
    now = datetime.now(timezone.utc)
    start, end, label = self._usage_period(period, now)
    with self._connect() as connection:
      metadata = self._metadata(connection)
      if not self._table_exists(connection, "channel_usage"):
        return {
          "period": period, "period_label": label, "circuits": [],
          "priced_energy_percent": 0, "available_months": [],
        }

      hourly_prices, half_hour_prices, _ = self._stored_market_prices(connection)

      totals: dict[int, dict[str, Any]] = {}
      for row in connection.execute(
        "SELECT id, period_start, energy_wh FROM channel_usage "
        "WHERE period_end>? AND period_start<? ORDER BY period_start, id",
        (start.isoformat(), end.isoformat()),
      ):
        channel_id = row["id"]
        name = metadata.get(channel_id, f"Channel {channel_id}")
        if name.casefold() in SUMMARY_NAMES or name.casefold().startswith("channel "):
          continue
        energy_kwh = float(row["energy_wh"]) / 1000
        market_price = self._market_price(
          datetime.fromisoformat(row["period_start"]),
          hourly_prices,
          half_hour_prices,
        )
        total = totals.setdefault(channel_id, {
          "channel_id": channel_id,
          "channel_name": name,
          "energy_kwh": 0.0,
          "priced_energy_kwh": 0.0,
          "cost_dollars": 0.0,
        })
        total["energy_kwh"] += energy_kwh
        if market_price is not None:
          total["priced_energy_kwh"] += energy_kwh
          total["cost_dollars"] += (
            energy_kwh * (market_price + variable_addon_cents_per_kwh) / 100
          )

      all_rows = sorted(
        totals.values(), key=lambda item: item["energy_kwh"], reverse=True
      )
      total_energy = sum(item["energy_kwh"] for item in all_rows)
      priced_energy = sum(item["priced_energy_kwh"] for item in all_rows)
      for item in all_rows:
        if item["priced_energy_kwh"] == 0:
          item["cost_dollars"] = None
        item["energy_kwh"] = round(item["energy_kwh"], 4)
        item.pop("priced_energy_kwh")

      return {
        "period": period,
        "period_label": label,
        "period_start_ms": round(start.timestamp() * 1000),
        "period_end_ms": round(end.timestamp() * 1000),
        "circuits": all_rows[:20],
        "priced_energy_percent": round(
          priced_energy / total_energy * 100, 1
        ) if total_energy else 0,
        "available_months": self._available_months(connection),
      }

  def circuit_history(
    self,
    circuit_id: int,
    period: str = "30d",
    *,
    variable_addon_cents_per_kwh: float = 7.791438,
  ) -> dict[str, Any]:
    """Return daily or monthly energy and variable-cost totals for a circuit."""
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(CHICAGO)
    today = local_now.date()

    if period in {"30d", "90d"}:
      day_count = 30 if period == "30d" else 90
      first_date = today - timedelta(days=day_count - 1)
      local_start = datetime.combine(first_date, datetime.min.time(), CHICAGO)
      local_end = datetime.combine(today + timedelta(days=1), datetime.min.time(), CHICAGO)
      keys = [first_date + timedelta(days=index) for index in range(day_count)]
      totals = {
        key: {"energy_kwh": 0.0, "priced_energy_kwh": 0.0, "cost_dollars": 0.0}
        for key in keys
      }
      granularity = "day"
      label = f"Last {day_count} days"
    elif re.fullmatch(r"year:\d{4}", period):
      year = int(period.split(":", 1)[1])
      local_start = datetime(year, 1, 1, tzinfo=CHICAGO)
      local_end = datetime(year + 1, 1, 1, tzinfo=CHICAGO)
      keys = [(year, month) for month in range(1, 13)]
      totals = {
        key: {"energy_kwh": 0.0, "priced_energy_kwh": 0.0, "cost_dollars": 0.0}
        for key in keys
      }
      granularity = "month"
      label = str(year)
    else:
      raise ValueError("Unknown circuit-history period")

    start = local_start.astimezone(timezone.utc)
    end = local_end.astimezone(timezone.utc)
    with self._connect() as connection:
      metadata = self._metadata(connection)
      if circuit_id not in metadata:
        raise ValueError("Unknown circuit")

      hourly_prices, half_hour_prices, latest_price = self._stored_market_prices(
        connection
      )

      if self._table_exists(connection, "channel_usage"):
        for row in connection.execute(
          "SELECT period_start, energy_wh FROM channel_usage "
          "WHERE id=? AND period_end>? AND period_start<? ORDER BY period_start",
          (circuit_id, start.isoformat(), end.isoformat()),
        ):
          sampled_at = datetime.fromisoformat(row["period_start"]).astimezone(CHICAGO)
          key = sampled_at.date() if granularity == "day" else (
            sampled_at.year, sampled_at.month
          )
          if key in totals:
            energy_kwh = float(row["energy_wh"]) / 1000
            totals[key]["energy_kwh"] += energy_kwh
            market_price = self._market_price(
              datetime.fromisoformat(row["period_start"]),
              hourly_prices,
              half_hour_prices,
            )
            if market_price is not None:
              totals[key]["priced_energy_kwh"] += energy_kwh
              totals[key]["cost_dollars"] += (
                energy_kwh
                * (market_price + variable_addon_cents_per_kwh)
                / 100
              )

      if self._table_exists(connection, "collector_state"):
        state_row = connection.execute(
          "SELECT state_json FROM collector_state WHERE id=1"
        ).fetchone()
        if state_row:
          state = json.loads(state_row["state_json"])
          state_start = state.get("period_start")
          energy = state.get("energy_watt_seconds", {})
          if state_start and str(circuit_id) in energy:
            sampled_at = datetime.fromisoformat(state_start).astimezone(CHICAGO)
            key = sampled_at.date() if granularity == "day" else (
              sampled_at.year, sampled_at.month
            )
            if key in totals:
              energy_kwh = float(energy[str(circuit_id)]) / 3_600_000
              totals[key]["energy_kwh"] += energy_kwh
              market_price = self._market_price(
                datetime.fromisoformat(state_start),
                hourly_prices,
                half_hour_prices,
              )
              if market_price is None and latest_price is not None:
                market_price = latest_price[1]
              if market_price is not None:
                totals[key]["priced_energy_kwh"] += energy_kwh
                totals[key]["cost_dollars"] += (
                  energy_kwh
                  * (market_price + variable_addon_cents_per_kwh)
                  / 100
                )

      years = []
      if self._table_exists(connection, "channel_usage"):
        years = [
          int(row["year"])
          for row in connection.execute(
            "SELECT DISTINCT substr(period_start, 1, 4) AS year "
            "FROM channel_usage ORDER BY year DESC"
          )
          if row["year"] and row["year"].isdigit()
        ]

    bars = []
    for key in keys:
      if granularity == "day":
        bar_start = datetime.combine(key, datetime.min.time(), CHICAGO)
        bar_label = key.strftime("%b %-d")
      else:
        bar_start = datetime(key[0], key[1], 1, tzinfo=CHICAGO)
        bar_label = bar_start.strftime("%b")
      energy_kwh = totals[key]["energy_kwh"]
      priced_energy_kwh = totals[key]["priced_energy_kwh"]
      bars.append({
        "period_start_ms": round(bar_start.timestamp() * 1000),
        "label": bar_label,
        "energy_kwh": round(energy_kwh, 4),
        "cost_dollars": (
          round(totals[key]["cost_dollars"], 6)
          if priced_energy_kwh > 0 else (0.0 if energy_kwh == 0 else None)
        ),
        "priced_energy_percent": round(
          priced_energy_kwh / energy_kwh * 100, 1
        ) if energy_kwh else 100.0,
        "current": (
          key == today if granularity == "day"
          else key == (today.year, today.month)
        ),
      })

    return {
      "circuit_id": circuit_id,
      "circuit_name": metadata[circuit_id],
      "period": period,
      "period_label": label,
      "granularity": granularity,
      "bars": bars,
      "available_years": years,
    }

  def solar_history(self, period: str = "14d") -> dict[str, Any]:
    """Return daily or monthly solar-production bars for a selected period."""
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(CHICAGO)
    today = local_now.date()
    granularity = "day"
    live = period in {"14d", "30d"}
    if period in {"14d", "30d"}:
      day_count = 14 if period == "14d" else 30
      start_date = today - timedelta(days=day_count - 1)
      local_start = datetime.combine(start_date, datetime.min.time(), CHICAGO)
      local_end = datetime.combine(today + timedelta(days=1), datetime.min.time(), CHICAGO)
      label = f"Last {day_count} days"
    elif period == "12m":
      month_index = local_now.year * 12 + local_now.month - 1 - 11
      start_year, start_month_index = divmod(month_index, 12)
      local_start = datetime(start_year, start_month_index + 1, 1, tzinfo=CHICAGO)
      local_end = self._next_month(datetime(
        local_now.year, local_now.month, 1, tzinfo=CHICAGO
      ))
      label = "Last 12 months"
      granularity = "month"
      live = False
    elif re.fullmatch(r"month:\d{4}-\d{2}", period):
      value = period.removeprefix("month:")
      year, month = (int(item) for item in value.split("-"))
      if not 1 <= month <= 12:
        raise ValueError("Unknown solar-history period")
      local_start = datetime(year, month, 1, tzinfo=CHICAGO)
      local_end = self._next_month(local_start)
      label = local_start.strftime("%B %Y")
    elif re.fullmatch(r"year:\d{4}", period):
      year = int(period.removeprefix("year:"))
      local_start = datetime(year, 1, 1, tzinfo=CHICAGO)
      local_end = datetime(year + 1, 1, 1, tzinfo=CHICAGO)
      label = str(year)
      granularity = "month"
    else:
      raise ValueError("Unknown solar-history period")

    with self._connect() as connection:
      metadata = self._metadata(connection)
      solar_ids = {
        channel_id for channel_id, name in metadata.items()
        if name.casefold() == "solar panels"
      }
      energy_by_date: dict[Any, float] = {}
      measured_by_slot: dict[tuple[Any, int, int], float] = {}
      if solar_ids and self._table_exists(connection, "channel_usage"):
        placeholders = ",".join("?" for _ in solar_ids)
        rows = connection.execute(
          f"SELECT period_start, energy_wh, measured_seconds FROM channel_usage "
          f"WHERE id IN ({placeholders}) AND period_start>=? AND period_start<? "
          "ORDER BY period_start",
          (
            *sorted(solar_ids),
            local_start.astimezone(timezone.utc).isoformat(),
            local_end.astimezone(timezone.utc).isoformat(),
          ),
        )
        for row in rows:
          sampled_at = datetime.fromisoformat(row["period_start"]).astimezone(CHICAGO)
          local_date = sampled_at.date()
          energy_by_date[local_date] = (
            energy_by_date.get(local_date, 0.0) + float(row["energy_wh"]) / 1000
          )
          key = (local_date, sampled_at.hour, sampled_at.minute)
          measured_by_slot[key] = max(
            measured_by_slot.get(key, 0.0), float(row["measured_seconds"])
          )

      if (
        solar_ids
        and local_start.date() <= today < local_end.date()
        and self._table_exists(connection, "collector_state")
      ):
        row = connection.execute(
          "SELECT state_json FROM collector_state WHERE id=1"
        ).fetchone()
        if row:
          state = json.loads(row["state_json"])
          state_start = state.get("period_start")
          if state_start:
            sampled_at = datetime.fromisoformat(state_start).astimezone(CHICAGO)
            if sampled_at.date() == today:
              energy = state.get("energy_watt_seconds", {})
              measured = state.get("measured_seconds", {})
              energy_by_date[today] = energy_by_date.get(today, 0.0) + sum(
                float(energy.get(str(channel_id), 0)) for channel_id in solar_ids
              ) / 3_600_000
              key = (today, sampled_at.hour, 0 if sampled_at.minute < 30 else 30)
              measured_by_slot[key] = max(
                measured_by_slot.get(key, 0.0),
                max(
                  (float(measured.get(str(channel_id), 0)) for channel_id in solar_ids),
                  default=0.0,
                ),
              )

      final_date = min(local_end.date() - timedelta(days=1), today)
      daily = []
      cursor = local_start.date()
      while cursor <= final_date:
        sunrise, sunset, window_start, window_end = self._solar_window(cursor)
        observed_end = (
          min(window_end, max(window_start, local_now))
          if cursor == today else window_end
        )
        expected_seconds = max(0.0, (observed_end - window_start).total_seconds())
        measured_seconds = sum(
          seconds for (date, hour, minute), seconds in measured_by_slot.items()
          if date == cursor and window_start <= datetime.combine(
            date, datetime.min.time(), CHICAGO
          ).replace(hour=hour, minute=minute) < window_end
        )
        measured_seconds = min(measured_seconds, expected_seconds)
        solar_active = (
          cursor == today
          and sunrise is not None
          and sunset is not None
          and sunrise <= local_now < sunset
        )
        daily.append({
          "date": cursor.isoformat(),
          "label": cursor.strftime("%a %-m/%-d"),
          "energy_kwh": round(energy_by_date.get(cursor, 0.0), 4),
          "coverage_percent": round(
            measured_seconds / expected_seconds * 100, 1
          ) if expected_seconds else 100.0,
          "missing_minutes": round(
            max(0.0, expected_seconds - measured_seconds) / 60, 1
          ),
          "expected_minutes": round(expected_seconds / 60, 1),
          "current": solar_active,
        })
        cursor += timedelta(days=1)

      if granularity == "day":
        bars = daily
      else:
        monthly: dict[str, dict[str, Any]] = {}
        for item in daily:
          month = item["date"][:7]
          total = monthly.setdefault(month, {
            "month": month,
            "label": datetime.strptime(month, "%Y-%m").strftime("%b %Y"),
            "energy_kwh": 0.0,
            "expected_minutes": 0.0,
            "missing_minutes": 0.0,
            "current": month == today.strftime("%Y-%m"),
          })
          total["energy_kwh"] += item["energy_kwh"]
          total["expected_minutes"] += item["expected_minutes"]
          total["missing_minutes"] += item["missing_minutes"]
        bars = []
        for item in monthly.values():
          expected = item.pop("expected_minutes")
          item["energy_kwh"] = round(item["energy_kwh"], 4)
          item["missing_minutes"] = round(item["missing_minutes"], 1)
          item["coverage_percent"] = round(
            (expected - item["missing_minutes"]) / expected * 100, 1
          ) if expected else 100.0
          bars.append(item)

      months = self._available_months(connection)
      years = sorted({item["value"][:4] for item in months}, reverse=True)
      today_sunrise, today_sunset, _, _ = self._solar_window(today)
      if today_sunrise and local_now < today_sunrise:
        daylight_status = "before_sunrise"
      elif today_sunset and local_now >= today_sunset:
        daylight_status = "after_sunset"
      else:
        daylight_status = "daylight"
      contains_today = local_start.date() <= today < local_end.date()
      live = live and daylight_status == "daylight"
      return {
        "period": period,
        "period_label": label,
        "granularity": granularity,
        "live": live,
        "contains_today": contains_today,
        "daylight_status": daylight_status,
        "bars": bars,
        "total_energy_kwh": round(sum(item["energy_kwh"] for item in bars), 4),
        "available_months": months,
        "available_years": years,
      }

  @staticmethod
  def _next_month(value: datetime) -> datetime:
    return (
      datetime(value.year + 1, 1, 1, tzinfo=value.tzinfo)
      if value.month == 12
      else datetime(value.year, value.month + 1, 1, tzinfo=value.tzinfo)
    )

  def _solar_summary(
    self,
    connection: sqlite3.Connection,
    metadata: dict[int, str],
    now: datetime,
  ) -> dict[str, Any]:
    """Build today's half-hour and recent daily solar-production totals."""
    solar_ids = {
      channel_id for channel_id, name in metadata.items()
      if name.casefold() == "solar panels"
    }
    local_now = now.astimezone(CHICAGO)
    today = local_now.date()
    first_day = today - timedelta(days=13)
    query_start = datetime.combine(
      first_day, datetime.min.time(), CHICAGO
    ).astimezone(timezone.utc)
    query_end = datetime.combine(
      today + timedelta(days=1), datetime.min.time(), CHICAGO
    ).astimezone(timezone.utc)
    daily_energy = {
      first_day + timedelta(days=offset): 0.0 for offset in range(14)
    }
    measured_by_slot: dict[tuple[Any, int, int], float] = {}
    energy_by_slot: dict[tuple[Any, int, int], float] = {}
    half_hour_energy: dict[datetime, float] = {}

    if solar_ids and self._table_exists(connection, "channel_usage"):
      placeholders = ",".join("?" for _ in solar_ids)
      rows = connection.execute(
        f"SELECT period_start, energy_wh, measured_seconds FROM channel_usage "
        f"WHERE id IN ({placeholders}) AND period_start>=? AND period_start<? "
        "ORDER BY period_start",
        (*sorted(solar_ids), query_start.isoformat(), query_end.isoformat()),
      )
      for row in rows:
        period_start = datetime.fromisoformat(row["period_start"]).astimezone(CHICAGO)
        energy_kwh = float(row["energy_wh"]) / 1000
        local_date = period_start.date()
        if local_date in daily_energy:
          daily_energy[local_date] += energy_kwh
          key = (local_date, period_start.hour, period_start.minute)
          energy_by_slot[key] = energy_by_slot.get(key, 0.0) + energy_kwh
          measured_by_slot[key] = max(
            measured_by_slot.get(key, 0.0), float(row["measured_seconds"])
          )
        if local_date == today:
          slot = period_start.replace(second=0, microsecond=0)
          half_hour_energy[slot] = half_hour_energy.get(slot, 0.0) + energy_kwh

    current_start: datetime | None = None
    current_energy_kwh = 0.0
    current_measured_seconds = 0.0
    if solar_ids and self._table_exists(connection, "collector_state"):
      row = connection.execute(
        "SELECT state_json FROM collector_state WHERE id=1"
      ).fetchone()
      if row:
        state = json.loads(row["state_json"])
        if state.get("period_start"):
          current_start = datetime.fromisoformat(
            state["period_start"]
          ).astimezone(CHICAGO)
        energy = state.get("energy_watt_seconds", {})
        measured = state.get("measured_seconds", {})
        current_energy_kwh = sum(
          float(energy.get(str(channel_id), 0)) for channel_id in solar_ids
        ) / 3_600_000
        current_measured_seconds = max(
          (float(measured.get(str(channel_id), 0)) for channel_id in solar_ids),
          default=0.0,
        )
    if current_start and current_start.date() == today:
      current_start = current_start.replace(
        minute=0 if current_start.minute < 30 else 30,
        second=0,
        microsecond=0,
      )
      daily_energy[today] += current_energy_kwh
      current_key = (today, current_start.hour, current_start.minute)
      energy_by_slot[current_key] = (
        energy_by_slot.get(current_key, 0.0) + current_energy_kwh
      )
      measured_by_slot[current_key] = max(
        measured_by_slot.get(current_key, 0.0), current_measured_seconds
      )
      half_hour_energy[current_start] = (
        half_hour_energy.get(current_start, 0.0) + current_energy_kwh
      )

    sunrise, sunset, solar_day_start, solar_day_end = self._solar_window(today)
    if sunrise and local_now < sunrise:
      daylight_status = "before_sunrise"
    elif sunset and local_now >= sunset:
      daylight_status = "after_sunset"
    else:
      daylight_status = "daylight"
    half_hours = []
    slot_count = round((solar_day_end - solar_day_start).total_seconds() / 1800)
    for offset in range(slot_count):
      slot_start = solar_day_start + timedelta(minutes=30 * offset)
      half_hours.append({
        "period_start_ms": round(slot_start.timestamp() * 1000),
        "energy_kwh": round(half_hour_energy.get(slot_start, 0.0), 4),
        "average_power_kw": round(
          (
            half_hour_energy.get(slot_start, 0.0)
            / (current_measured_seconds / 3600)
            if current_start == slot_start and current_measured_seconds > 0
            else half_hour_energy.get(slot_start, 0.0) * 2
          ),
          3,
        ),
        "current": (
          current_start == slot_start and daylight_status == "daylight"
        ),
        "future": slot_start > local_now,
      })

    days = []
    for local_date, energy_kwh in daily_energy.items():
      _, _, window_start, window_end = self._solar_window(local_date)
      observed_end = (
        min(window_end, max(window_start, local_now))
        if local_date == today else window_end
      )
      expected_seconds = max(0.0, (observed_end - window_start).total_seconds())
      measured_seconds = sum(
        seconds for (date, hour, minute), seconds in measured_by_slot.items()
        if date == local_date and window_start <= datetime.combine(
          date, datetime.min.time(), CHICAGO
        ).replace(hour=hour, minute=minute) < window_end
      )
      measured_seconds = min(measured_seconds, expected_seconds)
      missing_minutes = max(0.0, expected_seconds - measured_seconds) / 60
      days.append({
        "date": local_date.isoformat(),
        "label": local_date.strftime("%a %-m/%-d"),
        "energy_kwh": round(energy_kwh, 4),
        "current": local_date == today and daylight_status == "daylight",
        "coverage_percent": round(
          measured_seconds / expected_seconds * 100, 1
        ) if expected_seconds else 100.0,
        "missing_minutes": round(missing_minutes, 1),
      })

    yesterday = today - timedelta(days=1)
    current_slot_minute = local_now.hour * 60 + local_now.minute
    slot_progress = ((local_now.minute % 30) * 60 + local_now.second) / 1800
    yesterday_by_now = 0.0
    for (local_date, hour, minute), energy_kwh in energy_by_slot.items():
      if local_date != yesterday:
        continue
      slot_minute = hour * 60 + minute
      if slot_minute < current_slot_minute - local_now.minute % 30:
        yesterday_by_now += energy_kwh
      elif slot_minute == current_slot_minute - local_now.minute % 30:
        yesterday_by_now += energy_kwh * slot_progress

    difference_percent = None
    if yesterday_by_now > 0:
      difference_percent = (
        (daily_energy[today] - yesterday_by_now) / yesterday_by_now * 100
      )
    best_day = max(days, key=lambda item: item["energy_kwh"], default=None)
    return {
      "today_date": today.isoformat(),
      "today_energy_kwh": round(daily_energy[today], 4),
      "yesterday_by_now_kwh": round(yesterday_by_now, 4),
      "difference_percent": round(difference_percent, 1) if difference_percent is not None else None,
      "best_day": best_day,
      "sunrise_ms": round(sunrise.timestamp() * 1000) if sunrise else None,
      "sunset_ms": round(sunset.timestamp() * 1000) if sunset else None,
      "horizon_start_ms": round(solar_day_start.timestamp() * 1000),
      "horizon_end_ms": round(solar_day_end.timestamp() * 1000),
      "daylight_status": daylight_status,
      "today_half_hours": half_hours,
      "daily": days,
    }

  def _sun_event(self, local_date: Any, *, sunrise: bool) -> datetime | None:
    """Approximate a sunrise or sunset using the NOAA solar calculation."""
    day = local_date.timetuple().tm_yday
    longitude_hour = self.longitude / 15
    approximate = day + (
      (6 if sunrise else 18) - longitude_hour
    ) / 24
    mean_anomaly = 0.9856 * approximate - 3.289
    true_longitude = (
      mean_anomaly
      + 1.916 * math.sin(math.radians(mean_anomaly))
      + 0.020 * math.sin(math.radians(2 * mean_anomaly))
      + 282.634
    ) % 360
    right_ascension = math.degrees(math.atan(
      0.91764 * math.tan(math.radians(true_longitude))
    )) % 360
    right_ascension += (
      math.floor(true_longitude / 90) * 90
      - math.floor(right_ascension / 90) * 90
    )
    right_ascension /= 15
    sin_declination = 0.39782 * math.sin(math.radians(true_longitude))
    cos_declination = math.cos(math.asin(sin_declination))
    cos_hour = (
      math.cos(math.radians(90.833))
      - sin_declination * math.sin(math.radians(self.latitude))
    ) / (cos_declination * math.cos(math.radians(self.latitude)))
    if cos_hour < -1 or cos_hour > 1:
      return None
    hour_angle = math.degrees(math.acos(cos_hour))
    if sunrise:
      hour_angle = 360 - hour_angle
    hour_angle /= 15
    local_mean_time = (
      hour_angle + right_ascension - 0.06571 * approximate - 6.622
    )
    utc_hour = (local_mean_time - longitude_hour) % 24
    utc_midnight = datetime.combine(
      local_date, datetime.min.time(), timezone.utc
    )
    return (utc_midnight + timedelta(hours=utc_hour)).astimezone(CHICAGO)

  def _solar_window(
    self, local_date: Any
  ) -> tuple[datetime | None, datetime | None, datetime, datetime]:
    """Return sunrise/sunset and their outward-rounded half-hour window."""
    sunrise = self._sun_event(local_date, sunrise=True)
    sunset = self._sun_event(local_date, sunrise=False)
    fallback = datetime.combine(local_date, datetime.min.time(), CHICAGO)
    if sunrise is None or sunset is None:
      return sunrise, sunset, fallback.replace(hour=7), fallback.replace(hour=19)
    window_start = sunrise.replace(
      minute=0 if sunrise.minute < 30 else 30,
      second=0,
      microsecond=0,
    )
    window_end = sunset.replace(
      minute=0 if sunset.minute < 30 else 30,
      second=0,
      microsecond=0,
    )
    if window_end < sunset:
      window_end += timedelta(minutes=30)
    return sunrise, sunset, window_start, window_end

  def _data_quality(
    self,
    connection: sqlite3.Connection,
    metadata: dict[int, str],
    now: datetime,
  ) -> dict[str, Any]:
    """Calculate elapsed-time coverage using logical Main Power readings."""
    main_ids = {
      channel_id for channel_id, name in metadata.items()
      if name.casefold() == "main power"
    }
    state: dict[str, Any] = {}
    if self._table_exists(connection, "collector_state"):
      row = connection.execute(
        "SELECT state_json FROM collector_state WHERE id=1"
      ).fetchone()
      if row:
        state = json.loads(row["state_json"])

    local_today = now.astimezone(CHICAGO).date()
    results = []
    for days_ago, label in ((0, "Today"), (1, "Yesterday")):
      local_date = local_today - timedelta(days=days_ago)
      next_date = local_date + timedelta(days=1)
      start = datetime.combine(local_date, datetime.min.time(), CHICAGO).astimezone(
        timezone.utc
      )
      day_end = datetime.combine(
        next_date, datetime.min.time(), CHICAGO
      ).astimezone(timezone.utc)
      observed_end = min(day_end, now)
      expected_seconds = max(0.0, (observed_end - start).total_seconds())
      measured_seconds = 0.0

      if main_ids and self._table_exists(connection, "channel_usage"):
        placeholders = ",".join("?" for _ in main_ids)
        row = connection.execute(
          f"SELECT COALESCE(SUM(measured_seconds), 0) AS measured_seconds "
          f"FROM channel_usage WHERE id IN ({placeholders}) "
          "AND period_start<? AND period_end>?",
          (*sorted(main_ids), observed_end.isoformat(), start.isoformat()),
        ).fetchone()
        measured_seconds = float(row["measured_seconds"])

      state_start = state.get("period_start")
      state_end = state.get("period_end")
      if state_start and state_end and main_ids:
        period_start = datetime.fromisoformat(state_start)
        period_end = datetime.fromisoformat(state_end)
        if period_start < observed_end and period_end > start:
          current_measured = state.get("measured_seconds", {})
          measured_seconds += sum(
            float(current_measured.get(str(channel_id), 0))
            for channel_id in main_ids
          )

      measured_seconds = min(measured_seconds, expected_seconds)
      missing_seconds = max(0.0, expected_seconds - measured_seconds)
      results.append({
        "label": label,
        "date": local_date.isoformat(),
        "missing_minutes": round(missing_seconds / 60, 1),
        "expected_minutes": round(expected_seconds / 60, 1),
        "coverage_percent": round(
          measured_seconds / expected_seconds * 100, 1
        ) if expected_seconds else 100.0,
      })
    return {"days": results, "reference_circuit": "Main Power"}

  def snapshot(
    self,
    circuit_id: int | None = None,
    *,
    variable_addon_cents_per_kwh: float = 7.791438,
  ) -> dict[str, Any]:
    """Return current readings and 24 hours of history for one circuit."""
    now = datetime.now(timezone.utc)
    start_ms = round((now - timedelta(hours=24)).timestamp() * 1000)

    with self._connect() as connection:
      metadata = self._metadata(connection)
      has_samples = self._table_exists(connection, "power_samples")
      latest_ms: int | None = None
      current: list[dict[str, Any]] = []

      if has_samples:
        latest_row = connection.execute(
          "SELECT MAX(sampled_at_ms) AS sampled_at_ms FROM power_samples"
        ).fetchone()
        latest_ms = latest_row["sampled_at_ms"] if latest_row else None
        if latest_ms is not None:
          current = [
            {
              "channel_id": row["id"],
              "channel_name": metadata.get(row["id"], f"Channel {row['id']}"),
              "power_watts": row["power_watts"],
            }
            for row in connection.execute(
              "SELECT id, power_watts FROM power_samples "
              "WHERE sampled_at_ms=? ORDER BY id",
              (latest_ms,),
            )
          ]

      if not current:
        latest_ms, current = self._checkpoint_readings(connection)

      visible = [
        reading for reading in current
        if not reading["channel_name"].casefold().startswith("channel ")
      ]
      circuits = sorted(
        (
          {
            "id": reading["channel_id"],
            "name": reading["channel_name"],
          }
          for reading in visible
        ),
        key=lambda item: (
          0 if item["name"].casefold() == "main power" else 1,
          item["name"].casefold(),
        ),
      )

      selectable_ids = {item["id"] for item in circuits}
      if circuit_id not in selectable_ids:
        circuit_id = self._default_circuit(visible)

      history: list[dict[str, int]] = []
      if has_samples and circuit_id is not None:
        history = [
          {"timestamp_ms": row["sampled_at_ms"], "watts": row["power_watts"]}
          for row in connection.execute(
            "SELECT sampled_at_ms, power_watts FROM power_samples "
            "WHERE id=? AND sampled_at_ms>=? ORDER BY sampled_at_ms",
            (circuit_id, start_ms),
          )
        ]

      current_by_name = {
        item["channel_name"].casefold(): item for item in visible
      }
      pricing, last_24_hours = self._cost_summary(
        connection,
        metadata,
        now,
        current_by_name.get("main power", {}).get("power_watts"),
        variable_addon_cents_per_kwh,
      )
      data_quality = self._data_quality(connection, metadata, now)
      solar = self._solar_summary(connection, metadata, now)

    top_circuits = sorted(
      (
        item for item in visible
        if item["channel_name"].casefold() not in SUMMARY_NAMES
      ),
      key=lambda item: item["power_watts"],
      reverse=True,
    )[:5]
    selected = next((item for item in circuits if item["id"] == circuit_id), None)

    return {
      "sampled_at_ms": latest_ms,
      "main_power_watts": current_by_name.get("main power", {}).get("power_watts"),
      "solar_power_watts": current_by_name.get("solar panels", {}).get("power_watts"),
      "top_circuits": top_circuits,
      "circuits": circuits,
      "selected_circuit": selected,
      "history": history,
      "history_available": has_samples,
      "pricing": pricing,
      "last_24_hours": last_24_hours,
      "solar": solar,
      "data_quality": data_quality,
    }

  @staticmethod
  def _default_circuit(readings: list[dict[str, Any]]) -> int | None:
    main_power = next(
      (
        item for item in readings
        if item["channel_name"].casefold() == "main power"
      ),
      None,
    )
    if main_power:
      return main_power["channel_id"]
    candidates = [
      item for item in readings
      if item["channel_name"].casefold() not in SUMMARY_NAMES
    ]
    if not candidates:
      candidates = readings
    return max(candidates, key=lambda item: item["power_watts"], default={}).get(
      "channel_id"
    )


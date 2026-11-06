"""Read-only access to the eMonitor collector's SQLite database."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SUMMARY_NAMES = {"main power", "solar panels"}
CHICAGO = ZoneInfo("America/Chicago")


class DashboardDatabase:
  """Query collector data without modifying or blocking its database."""

  def __init__(self, database_path: str | Path):
    self.database_path = Path(database_path).expanduser().resolve()

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

  def _cost_summary(
    self,
    connection: sqlite3.Connection,
    metadata: dict[int, str],
    now: datetime,
    current_main_power_watts: int | None,
    live_pricing: dict[str, Any] | None,
    variable_addon_cents_per_kwh: float,
  ) -> tuple[dict[str, Any], dict[str, Any]]:
    hourly_prices: dict[int, float] = {}
    stored_latest: tuple[int, float] | None = None
    if self._table_exists(connection, "comed_hourly_prices"):
      for row in connection.execute(
        "SELECT hour_start, price_cents_per_kwh "
        "FROM comed_hourly_prices ORDER BY hour_start"
      ):
        timestamp_ms = self._hour_start_ms(datetime.fromisoformat(row["hour_start"]))
        price = float(row["price_cents_per_kwh"])
        hourly_prices[timestamp_ms] = price
        stored_latest = (timestamp_ms, price)

    if live_pricing:
      hourly_prices.update({
        int(row["hour_start_ms"]): float(row["price_cents_per_kwh"])
        for row in live_pricing.get("hourly_prices", [])
      })

    current_market_price = (
      live_pricing.get("current_price_cents_per_kwh")
      if live_pricing else None
    )
    price_at_ms = live_pricing.get("price_at_ms") if live_pricing else None
    source = live_pricing.get("source") if live_pricing else "Collector price table"
    if current_market_price is None and stored_latest:
      price_at_ms, current_market_price = stored_latest

    effective_rate = (
      current_market_price + variable_addon_cents_per_kwh
      if current_market_price is not None else None
    )
    current_cost_per_hour = (
      current_main_power_watts / 1000 * effective_rate / 100
      if current_main_power_watts is not None and effective_rate is not None
      else None
    )
    pricing = {
      "market_price_cents_per_kwh": current_market_price,
      "variable_addon_cents_per_kwh": variable_addon_cents_per_kwh,
      "effective_variable_rate_cents_per_kwh": effective_rate,
      "current_cost_dollars_per_hour": current_cost_per_hour,
      "price_at_ms": price_at_ms,
      "source": source,
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
        market_price = hourly_prices.get(self._hour_start_ms(period_start))
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
    live_pricing: dict[str, Any] | None = None,
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

      hourly_prices: dict[int, float] = {}
      if self._table_exists(connection, "comed_hourly_prices"):
        for row in connection.execute(
          "SELECT hour_start, price_cents_per_kwh FROM comed_hourly_prices"
        ):
          hourly_prices[self._hour_start_ms(
            datetime.fromisoformat(row["hour_start"])
          )] = float(row["price_cents_per_kwh"])
      if live_pricing:
        hourly_prices.update({
          int(row["hour_start_ms"]): float(row["price_cents_per_kwh"])
          for row in live_pricing.get("hourly_prices", [])
        })

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
        market_price = hourly_prices.get(self._hour_start_ms(
          datetime.fromisoformat(row["period_start"])
        ))
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
    half_hour_energy: dict[datetime, float] = {}

    if solar_ids and self._table_exists(connection, "channel_usage"):
      placeholders = ",".join("?" for _ in solar_ids)
      rows = connection.execute(
        f"SELECT period_start, energy_wh FROM channel_usage "
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
        if local_date == today:
          slot = period_start.replace(second=0, microsecond=0)
          half_hour_energy[slot] = half_hour_energy.get(slot, 0.0) + energy_kwh

    current_start: datetime | None = None
    current_energy_kwh = 0.0
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
        current_energy_kwh = sum(
          float(energy.get(str(channel_id), 0)) for channel_id in solar_ids
        ) / 3_600_000
    if current_start and current_start.date() == today:
      current_start = current_start.replace(
        minute=0 if current_start.minute < 30 else 30,
        second=0,
        microsecond=0,
      )
      daily_energy[today] += current_energy_kwh
      half_hour_energy[current_start] = (
        half_hour_energy.get(current_start, 0.0) + current_energy_kwh
      )

    solar_day_start = datetime.combine(today, datetime.min.time(), CHICAGO).replace(
      hour=7
    )
    half_hours = []
    for offset in range(24):
      slot_start = solar_day_start + timedelta(minutes=30 * offset)
      half_hours.append({
        "period_start_ms": round(slot_start.timestamp() * 1000),
        "energy_kwh": round(half_hour_energy.get(slot_start, 0.0), 4),
        "current": current_start == slot_start,
        "future": slot_start > local_now,
      })

    days = []
    for local_date, energy_kwh in daily_energy.items():
      days.append({
        "date": local_date.isoformat(),
        "label": local_date.strftime("%a %-m/%-d"),
        "energy_kwh": round(energy_kwh, 4),
        "current": local_date == today,
      })
    return {
      "today_date": today.isoformat(),
      "today_energy_kwh": round(daily_energy[today], 4),
      "today_half_hours": half_hours,
      "daily": days,
    }

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
    live_pricing: dict[str, Any] | None = None,
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
        live_pricing,
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


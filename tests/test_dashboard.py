import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app
from database import DashboardDatabase
from system_metrics import SystemMetrics


def make_database(path, include_samples=True):
  connection = sqlite3.connect(path)
  connection.executescript("""
    CREATE TABLE meta_data (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, input INTEGER NOT NULL,
      rating INTEGER NOT NULL, voltage INTEGER NOT NULL, paired INTEGER NOT NULL
    );
    CREATE TABLE collector_state (
      id INTEGER PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE channel_usage (
      period_start TEXT NOT NULL, period_end TEXT NOT NULL, id INTEGER NOT NULL,
      energy_wh REAL NOT NULL, measured_seconds REAL NOT NULL,
      average_power_watts REAL NOT NULL
    );
    CREATE TABLE comed_hourly_prices (
      hour_start TEXT PRIMARY KEY, price_cents_per_kwh REAL NOT NULL,
      source_sample_count INTEGER NOT NULL, updated_at TEXT NOT NULL
    );
  """)
  connection.executemany(
    "INSERT INTO meta_data VALUES (?, ?, 0, 20, 120, 0)",
    [(1, "Main Power"), (3, "Kitchen"), (5, "Car Charger"), (7, "Solar Panels")],
  )
  now = datetime.now(timezone.utc)
  readings = [
    {"channel_id": 1, "channel_name": "Main Power", "power_watts": 900},
    {"channel_id": 3, "channel_name": "Kitchen", "power_watts": 120},
    {"channel_id": 5, "channel_name": "Car Charger", "power_watts": 7100},
    {"channel_id": 7, "channel_name": "Solar Panels", "power_watts": 500},
  ]
  current_period_start = now.replace(
    minute=0 if now.minute < 30 else 30, second=0, microsecond=0
  )
  state = {
    "last_sample_time": now.isoformat(),
    "last_readings": readings,
    "period_start": current_period_start.isoformat(),
    "period_end": (current_period_start + timedelta(minutes=30)).isoformat(),
    "measured_seconds": {"7": 720},
    "energy_watt_seconds": {"7": 360_000},
  }
  connection.execute(
    "INSERT INTO collector_state VALUES (1, ?, ?)",
    (json.dumps(state), now.isoformat()),
  )
  period_start = now.replace(minute=0, second=0, microsecond=0)
  period_end = period_start + timedelta(minutes=30)
  connection.executemany(
    "INSERT INTO channel_usage VALUES (?, ?, ?, ?, ?, ?)",
    [
      (period_start.isoformat(), period_end.isoformat(), 1, 450, 1800, 900),
      (period_start.isoformat(), period_end.isoformat(), 3, 60, 1800, 120),
      (period_start.isoformat(), period_end.isoformat(), 5, 3550, 1800, 7100),
      (period_start.isoformat(), period_end.isoformat(), 7, 250, 1800, 500),
    ],
  )
  if include_samples:
    connection.executescript("""
      CREATE TABLE power_samples (
        sampled_at_ms INTEGER NOT NULL, id INTEGER NOT NULL,
        power_watts INTEGER NOT NULL, PRIMARY KEY (sampled_at_ms, id)
      ) WITHOUT ROWID;
    """)
    for offset, multiplier in [(-15, .9), (0, 1)]:
      timestamp = round((now + timedelta(seconds=offset)).timestamp() * 1000)
      connection.executemany(
        "INSERT INTO power_samples VALUES (?, ?, ?)",
        [(timestamp, item["channel_id"], round(item["power_watts"] * multiplier)) for item in readings],
      )
  connection.commit()
  connection.close()


def test_snapshot_returns_live_ranking_and_history(tmp_path):
  path = tmp_path / "emonitor.db"
  make_database(path)

  result = DashboardDatabase(path).snapshot(5)

  assert result["main_power_watts"] == 900
  assert result["solar_power_watts"] == 500
  assert result["top_circuits"][0]["channel_name"] == "Car Charger"
  assert result["selected_circuit"] == {"id": 5, "name": "Car Charger"}
  assert [point["watts"] for point in result["history"]] == [6390, 7100]
  assert result["solar"]["today_energy_kwh"] == pytest.approx(0.35)
  solar = result["solar"]
  assert len(solar["today_half_hours"]) > 0
  assert solar["today_half_hours"][0]["period_start_ms"] == solar["horizon_start_ms"]
  assert (
    solar["today_half_hours"][-1]["period_start_ms"] + 30 * 60 * 1000
    == solar["horizon_end_ms"]
  )
  assert all(
    right["period_start_ms"] - left["period_start_ms"] == 30 * 60 * 1000
    for left, right in zip(
      solar["today_half_hours"], solar["today_half_hours"][1:]
    )
  )
  assert len(result["solar"]["daily"]) == 14
  assert result["solar"]["sunrise_ms"] < result["solar"]["sunset_ms"]
  assert result["solar"]["daylight_status"] in {
    "before_sunrise", "daylight", "after_sunset"
  }
  assert "average_power_kw" in result["solar"]["today_half_hours"][0]
  assert "coverage_percent" in result["solar"]["daily"][-1]
  assert result["solar"]["best_day"] is not None


def test_main_power_is_default_and_first_selectable_circuit(tmp_path):
  path = tmp_path / "emonitor.db"
  make_database(path)

  result = DashboardDatabase(path).snapshot()

  assert result["selected_circuit"]["name"] == "Main Power"
  assert result["circuits"][0]["name"] == "Main Power"


def test_snapshot_falls_back_to_checkpoint_before_sample_table_exists(tmp_path):
  path = tmp_path / "emonitor.db"
  make_database(path, include_samples=False)

  result = DashboardDatabase(path).snapshot()

  assert result["history_available"] is False
  assert result["history"] == []
  assert result["top_circuits"][0]["channel_name"] == "Car Charger"


def test_api_returns_dashboard_payload(tmp_path):
  path = tmp_path / "emonitor.db"
  make_database(path)
  class FakePrices:
    def snapshot(self):
      hour_ms = round(datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0
      ).timestamp() * 1000)
      return {
        "current_price_cents_per_kwh": 4.25,
        "price_at_ms": hour_ms,
        "hourly_prices": [{
          "hour_start_ms": hour_ms, "price_cents_per_kwh": 4.0,
        }],
        "source": "test",
      }

  client = create_app(path, pricing_service=FakePrices()).test_client()

  response = client.get("/api/dashboard?circuit_id=3")

  assert response.status_code == 200
  assert response.get_json()["selected_circuit"]["name"] == "Kitchen"
  assert "system" in response.get_json()
  assert response.get_json()["pricing"]["market_price_cents_per_kwh"] == 4.25
  assert response.get_json()["last_24_hours"]["circuits"][0]["channel_name"] == "Car Charger"
  assert response.get_json()["pricing"]["effective_variable_rate_cents_per_kwh"] == pytest.approx(12.041438)
  assert response.get_json()["last_24_hours"]["circuits"][0]["cost_dollars"] == pytest.approx(0.418596049)
  assert response.get_json()["pricing"]["burn_rate_history"][0]["dollars_per_hour"] == pytest.approx(0.106122942)
  assert response.get_json()["pricing"]["burn_rate_history"][-1]["current"] is True
  assert [day["label"] for day in response.get_json()["data_quality"]["days"]] == ["Today", "Yesterday"]
  assert response.get_json()["data_quality"]["days"][0]["missing_minutes"] >= 0

  usage_response = client.get("/api/circuit-usage?period=7d")
  assert usage_response.status_code == 200
  usage = usage_response.get_json()
  assert usage["period_label"] == "Last 7 days"
  assert usage["circuits"][0]["channel_name"] == "Car Charger"
  assert usage["circuits"][0]["energy_kwh"] == pytest.approx(3.55)
  assert usage["available_months"]

  month = datetime.now(timezone.utc).strftime("%Y-%m")
  month_response = client.get(f"/api/circuit-usage?period={month}")
  assert month_response.status_code == 200
  assert month_response.get_json()["period"] == month

  invalid_response = client.get("/api/circuit-usage?period=forever")
  assert invalid_response.status_code == 400

  solar_response = client.get("/api/solar-history?period=14d")
  assert solar_response.status_code == 200
  solar_history = solar_response.get_json()
  assert solar_history["granularity"] == "day"
  assert len(solar_history["bars"]) == 14
  assert solar_history["bars"][-1]["energy_kwh"] == pytest.approx(0.35)

  monthly_response = client.get("/api/solar-history?period=12m")
  assert monthly_response.status_code == 200
  assert monthly_response.get_json()["granularity"] == "month"

  current_month = datetime.now(timezone.utc).strftime("%Y-%m")
  assert client.get(
    f"/api/solar-history?period=month:{current_month}"
  ).status_code == 200
  assert client.get("/api/solar-history?period=year:2026").status_code == 200
  assert client.get("/api/solar-history?period=forever").status_code == 400


def test_system_metrics_reads_pi_sysfs_values(tmp_path):
  thermal = tmp_path / "class" / "thermal" / "thermal_zone0"
  fan = tmp_path / "class" / "hwmon" / "hwmon0"
  thermal.mkdir(parents=True)
  fan.mkdir(parents=True)
  proc = tmp_path / "proc"
  proc.mkdir()
  (thermal / "type").write_text("cpu-thermal\n", encoding="utf-8")
  (thermal / "temp").write_text("52750\n", encoding="utf-8")
  (fan / "fan1_input").write_text("3180\n", encoding="utf-8")
  (proc / "stat").write_text("cpu  100 0 50 850 0 0 0 0\n", encoding="utf-8")
  (proc / "meminfo").write_text(
    "MemTotal:       8000000 kB\nMemAvailable:   6000000 kB\n",
    encoding="utf-8",
  )

  metrics = SystemMetrics(
    sysfs_root=tmp_path, proc_root=proc, disk_path=tmp_path
  )
  (proc / "stat").write_text("cpu  120 0 60 920 0 0 0 0\n", encoding="utf-8")
  result = metrics.snapshot()

  assert result["cpu_temperature_c"] == 52.8
  assert result["fan_speed_rpm"] == 3180
  assert result["cpu_utilization_percent"] == 30.0
  assert result["memory_used_percent"] == 25.0
  assert result["memory_used_bytes"] == 2_000_000 * 1024
  assert result["disk_free_bytes"] > 0


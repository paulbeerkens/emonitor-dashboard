"""Live dashboard for data collected by the eMonitor application."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from waitress import serve

from database import DashboardDatabase
from system_metrics import SystemMetrics


PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def create_app(
  database_path: str | Path | None = None,
  system_metrics: SystemMetrics | None = None,
) -> Flask:
  app = Flask(__name__)
  configured_path = database_path or os.getenv(
    "EMONITOR_DATABASE_PATH", "../emonitor/db/emonitor.db"
  )
  path = Path(configured_path)
  if not path.is_absolute():
    path = PROJECT_ROOT / path
  database = DashboardDatabase(
    path,
    latitude=float(os.getenv("SOLAR_LATITUDE", "41.8781")),
    longitude=float(os.getenv("SOLAR_LONGITUDE", "-87.6298")),
  )
  metrics = system_metrics or SystemMetrics()
  variable_addon = float(os.getenv(
    "COMED_VARIABLE_ADDON_CENTS_PER_KWH", "7.791438"
  ))

  @app.get("/")
  def dashboard():
    return render_template("dashboard.html")

  @app.get("/api/dashboard")
  def dashboard_data():
    requested_id = request.args.get("circuit_id", type=int)
    try:
      payload = database.snapshot(
        requested_id,
        variable_addon_cents_per_kwh=variable_addon,
      )
      payload["system"] = metrics.snapshot()
      return jsonify(payload)
    except (FileNotFoundError, sqlite3.DatabaseError, ValueError) as error:
      app.logger.warning("Could not read dashboard data: %s", error)
      return jsonify({"error": str(error)}), 503

  @app.get("/api/circuit-usage")
  def circuit_usage():
    period = request.args.get("period", "24h")
    try:
      return jsonify(database.circuit_usage(
        period,
        variable_addon_cents_per_kwh=variable_addon,
      ))
    except ValueError as error:
      return jsonify({"error": str(error)}), 400
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
      app.logger.warning("Could not read circuit usage: %s", error)
      return jsonify({"error": str(error)}), 503

  @app.get("/api/solar-history")
  def solar_history():
    period = request.args.get("period", "14d")
    try:
      return jsonify(database.solar_history(period))
    except ValueError as error:
      return jsonify({"error": str(error)}), 400
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
      app.logger.warning("Could not read solar history: %s", error)
      return jsonify({"error": str(error)}), 503

  @app.get("/api/circuit-history")
  def circuit_history():
    circuit_id = request.args.get("circuit_id", type=int)
    period = request.args.get("period", "30d")
    if circuit_id is None:
      return jsonify({"error": "circuit_id is required"}), 400
    try:
      return jsonify(database.circuit_history(circuit_id, period))
    except ValueError as error:
      return jsonify({"error": str(error)}), 400
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
      app.logger.warning("Could not read circuit history: %s", error)
      return jsonify({"error": str(error)}), 503

  return app


app = create_app()


if __name__ == "__main__":
  host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
  port = int(os.getenv("DASHBOARD_PORT", "5000"))
  print(f"eMonitor dashboard available at http://{host}:{port}", flush=True)
  serve(app, host=host, port=port, threads=4)


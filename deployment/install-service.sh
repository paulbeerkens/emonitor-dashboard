#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="emonitor-dashboard.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
RUN_USER="${SUDO_USER:-$USER}"
RUN_GROUP="$(id -gn "$RUN_USER")"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "The environment is missing. Run env/install.sh first."
  exit 1
fi
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo ".env is missing. Copy .env.example to .env and review it with vim."
  exit 1
fi

sed \
  -e "s|__USER__|$RUN_USER|g" \
  -e "s|__GROUP__|$RUN_GROUP|g" \
  -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  "$PROJECT_DIR/deployment/emonitor-dashboard.service.template" > "$SERVICE_PATH"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl --no-pager status "$SERVICE_NAME"


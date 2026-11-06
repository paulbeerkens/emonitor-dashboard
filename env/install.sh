#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3.13}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.13 is required. Install it, then run this script again."
  exit 1
fi

"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env. Review it with: vim $PROJECT_DIR/.env"
fi

echo "Dashboard environment is ready."
echo "Run it now with: .venv/bin/python app.py"
echo "For continuous Pi operation, run: sudo deployment/install-service.sh"


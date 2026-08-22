#!/usr/bin/env bash
set -euo pipefail

tool_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${APP_METRICS_DASHBOARD_PYTHON:-$tool_root/.venv/bin/python}"

if [[ ! -x "$python_bin" ]]; then
  echo "Missing dashboard environment at $tool_root/.venv" >&2
  echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

exec "$python_bin" "$tool_root/Scripts/serve_dashboard.py" "$@"

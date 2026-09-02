#!/usr/bin/env bash
# Starts the service on $PORT (default 8080). The upstream base URL is read
# from $FX_UPSTREAM_BASE inside main.py — nothing here hardcodes the host.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet -r requirements.txt

exec .venv/bin/uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}"

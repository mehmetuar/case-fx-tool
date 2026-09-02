#!/usr/bin/env bash
# Runs the tests. They pass with no network: the upstream is faked in-process,
# and one test deliberately targets an unreachable port to prove the service
# fails loudly instead of inventing a number.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet -r requirements.txt

exec .venv/bin/python -m pytest -q

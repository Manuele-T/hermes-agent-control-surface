#!/usr/bin/env bash
# One-command runner. Never run from native Windows.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ "$(uname -s)" != "Linux" && "$(uname -s)" != "Darwin" ]]; then
  echo "error: run this from Linux or macOS, not Windows." >&2
  exit 1
fi

if [[ ! -d backend/.venv ]]; then
  echo "Creating backend/.venv ..."
  python3 -m venv backend/.venv || {
    echo "error: 'python3 -m venv' failed — install the venv module first:" >&2
    echo "  sudo apt install python3.\$(python3 -c 'import sys; print(sys.version_info[1])')-venv" >&2
    exit 1
  }
fi
source backend/.venv/bin/activate
pip install -q -r backend/requirements.txt

if [[ ! -d frontend/node_modules ]]; then
  echo "Installing frontend dependencies ..."
  (cd frontend && npm install)
fi
echo "Building frontend ..."
(cd frontend && npm run build)

if [[ ! -f .env ]]; then
  echo "warning: no .env found — copy .env.example to .env and set HERMES_DASHBOARD_SESSION_TOKEN for writes." >&2
fi

cd backend
PORT="${PORT:-8124}"
echo "Starting on http://127.0.0.1:${PORT} ..."
exec uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
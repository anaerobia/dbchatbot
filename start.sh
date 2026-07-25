#!/usr/bin/env bash
#
# Start the DB Chatbot backend (FastAPI) and frontend (Vite) together.
# Ctrl+C stops both. Run from anywhere: ./start.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
BACKEND_PORT="${BACKEND_PORT:-8000}"

# --- preflight checks ---------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' is not installed. Install it from https://docs.astral.sh/uv/" >&2
  echo "  (e.g. 'curl -LsSf https://astral.sh/uv/install.sh | sh')" >&2
  exit 1
fi

# Ensure backend dependencies are installed (creates .venv from pyproject.toml
# on first run; a no-op once synced).
if [ ! -d "$BACKEND/.venv" ]; then
  echo "Syncing backend dependencies with uv (first run)..."
  ( cd "$BACKEND" && uv sync )
fi

if [ ! -f "$BACKEND/.env" ]; then
  echo "WARNING: $BACKEND/.env not found — creating from .env.example." >&2
  echo "         Edit it and set ANTHROPIC_API_KEY before asking questions." >&2
  cp "$BACKEND/.env.example" "$BACKEND/.env"
fi

if [ ! -d "$FRONTEND/node_modules" ]; then
  echo "Installing frontend dependencies (first run)..."
  (cd "$FRONTEND" && npm install)
fi

# --- start both processes -----------------------------------------------------
pids=()

# Recursively terminate a PID and all its descendants. `npm run dev` spawns
# vite as a child, so killing the npm PID alone would orphan vite — walk the
# tree via `pgrep -P` and kill children first.
kill_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_tree "$child"
  done
  kill -TERM "$pid" 2>/dev/null || true
}

cleanup() {
  trap - INT TERM EXIT  # avoid re-entry
  echo ""
  echo "Stopping servers..."
  if [ "${#pids[@]}" -gt 0 ]; then
    for pid in "${pids[@]}"; do
      kill_tree "$pid"
    done
  fi
  wait 2>/dev/null || true
  echo "Stopped."
}
trap cleanup INT TERM EXIT

echo "Starting backend on http://localhost:$BACKEND_PORT ..."
# 'uv run' uses the project's env (from pyproject.toml/uv.lock), no activation
# needed. Run from backend/ so it finds main.py and loads backend/.env.
( cd "$BACKEND" && exec uv run uvicorn main:app --port "$BACKEND_PORT" ) &
pids+=($!)

echo "Starting frontend (Vite) ..."
( cd "$FRONTEND" && exec npm run dev ) &
pids+=($!)

echo ""
echo "  Backend : http://localhost:$BACKEND_PORT   (health: /api/health)"
echo "  Frontend: http://localhost:5173"
echo "  Press Ctrl+C to stop both."
echo ""

# Wait until either process exits (portable to macOS bash 3.2 — no `wait -n`).
# When one dies, the loop ends and the EXIT trap stops the other.
while kill -0 "${pids[0]}" 2>/dev/null && kill -0 "${pids[1]}" 2>/dev/null; do
  sleep 1
done

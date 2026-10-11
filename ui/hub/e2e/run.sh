#!/usr/bin/env bash
# Full UI end-to-end run: build the UI, start the stack (Postgres from PG* env, Temporal dev
# server, worker, hub API), run Playwright with a virtual passkey, verify the database, then
# tear everything down.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"

cd "$HERE/.."
NEXT_TELEMETRY_DISABLED=1 npx next build >/dev/null

"$PY" "$HERE/stack.py" >"$HERE/.stack.log" 2>&1 &
STACK_PID=$!
cleanup() { kill "$STACK_PID" 2>/dev/null || true; wait "$STACK_PID" 2>/dev/null || true; }
trap cleanup EXIT
for _ in $(seq 1 120); do
  grep -q "stack ready" "$HERE/.stack.log" 2>/dev/null && break
  kill -0 "$STACK_PID" 2>/dev/null || { cat "$HERE/.stack.log"; exit 1; }
  sleep 1
done

npx playwright test
"$PY" "$HERE/verify.py"

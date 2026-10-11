#!/usr/bin/env bash
# Create a throwaway database, apply all migrations (twice, to prove the runner is idempotent),
# run the SQL guard tests, then drop it. Connection comes from the PG* env vars
# (PGHOST, PGPORT, PGUSER, PGPASSWORD). No credentials are hard-coded here.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python}"
DB="ytcore_guard_$$"
export PGDATABASE="${PGDATABASE:-postgres}"
ADMIN_DB="$PGDATABASE"

cleanup() { psql -X -q -d "$ADMIN_DB" -c "DROP DATABASE IF EXISTS $DB" >/dev/null 2>&1 || true; }
trap cleanup EXIT

psql -X -q -v ON_ERROR_STOP=1 -d "$ADMIN_DB" -c "CREATE DATABASE $DB"
unset DATABASE_URL
PGDATABASE="$DB" "$PY" -m ytcore.db.migrate
PGDATABASE="$DB" "$PY" -m ytcore.db.migrate >/dev/null
for t in tests/schema_guard*_test.sql; do
    PGDATABASE="$DB" psql -X -v ON_ERROR_STOP=1 -f "$t"
done

#!/usr/bin/env bash
# Backup drill on a throwaway database: migrate, back up (Postgres dump + storage manifest),
# restore into a scratch database and verify it against the backup manifest. Same code path as
# production (python -m ytcore.ops.backup). Connection comes from the PG* env vars.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python}"
DB="ytcore_drill_src_$$"
DIR="$(mktemp -d)"
export PGDATABASE="${PGDATABASE:-postgres}"
ADMIN_DB="$PGDATABASE"

cleanup() {
    psql -X -q -d "$ADMIN_DB" -c "DROP DATABASE IF EXISTS $DB WITH (FORCE)" >/dev/null 2>&1 || true
    rm -rf "$DIR"
}
trap cleanup EXIT

psql -X -q -v ON_ERROR_STOP=1 -d "$ADMIN_DB" -c "CREATE DATABASE $DB"
unset DATABASE_URL
PGDATABASE="$DB" "$PY" -m ytcore.db.migrate
mkdir -p "$DIR/assets/channels/drill/assets"
printf 'drill-object' >"$DIR/assets/channels/drill/assets/obj"
export BACKUP_CONNINFO="dbname=$DB"
"$PY" -m ytcore.ops.backup backup --dir "$DIR/backup" --assets-dir "$DIR/assets"
# The drill connects to the server (not the source database) to create the scratch database.
BACKUP_CONNINFO="dbname=$ADMIN_DB" "$PY" -m ytcore.ops.backup restore-drill --dir "$DIR/backup"

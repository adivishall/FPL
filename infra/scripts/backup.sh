#!/usr/bin/env bash
# Back up a Docker Compose deployment: the PostgreSQL database (pg_dump, custom format) and the
# irreplaceable part of the /data volume (canonical snapshots and raw source captures).
# Forecasts, price models and feature caches are not backed up: they are recomputed from the
# snapshots. Secrets (.env, auth.caddy) and TLS certificates are not backed up either: keep the
# secrets in your secret manager; certificates are re-issued automatically.
#
# Usage (repo root): infra/scripts/backup.sh [backup-root]          default: ./backups
#   COMPOSE="docker compose -f docker-compose.yml -f docker-compose.public.yml" infra/scripts/backup.sh
# Writes <backup-root>/<UTC timestamp>/{db.dump,db.toc,data.tar.gz,SHA256SUMS,manifest.txt}
# and fails (non-zero) if the dump cannot be read back.
set -euo pipefail

COMPOSE=${COMPOSE:-docker compose}
root=${1:-backups}
out="$root/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$out"
umask 077

# a consistent snapshot of the whole database (MVCC), taken while the stack keeps running
$COMPOSE exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$out/db.dump"
# readable: list its table of contents back through pg_restore
$COMPOSE exec -T postgres pg_restore --list < "$out/db.dump" > "$out/db.toc"
tables=$(grep -c " TABLE DATA " "$out/db.toc" || true)
[ "$tables" -gt 0 ] || { echo "backup: dump has no table data" >&2; exit 1; }

$COMPOSE exec -T api tar -C /data -czf - snapshots raw > "$out/data.tar.gz"
tar -tzf "$out/data.tar.gz" > /dev/null

(cd "$out" && shasum -a 256 db.dump data.tar.gz > SHA256SUMS)
{
  echo "created_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "alembic_version: $($COMPOSE exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select version_num from alembic_version"')"
  echo "tables_with_data: $tables"
  echo "db_dump_bytes: $(wc -c < "$out/db.dump" | tr -d ' ')"
  echo "data_archive_bytes: $(wc -c < "$out/data.tar.gz" | tr -d ' ')"
} > "$out/manifest.txt"
echo "$out"

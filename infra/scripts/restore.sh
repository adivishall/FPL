#!/usr/bin/env bash
# Restore a backup made by backup.sh into a deployment — typically a new host or fresh volumes.
# Replaces the database contents and the snapshots / raw captures in /data, migrates the schema
# to the current code, starts the stack and submits the serving forecast (the API serves the
# restored data once the worker has precomputed it, ~1–2 minutes).
#
# Usage (repo root, .env in place): infra/scripts/restore.sh backups/<UTC timestamp>
#   COMPOSE="docker compose -f docker-compose.yml -f docker-compose.public.yml" infra/scripts/restore.sh …
set -euo pipefail

COMPOSE=${COMPOSE:-docker compose}
src=${1:?usage: restore.sh <backup directory>}
(cd "$src" && shasum -a 256 -c SHA256SUMS)

$COMPOSE up -d --wait postgres redis
$COMPOSE stop api worker scheduler web 2>/dev/null || true   # nothing writes during the restore
$COMPOSE exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --exit-on-error' \
  < "$src/db.dump"
$COMPOSE run --rm migrate                                     # no-op when already at head
$COMPOSE run --rm --no-deps -T api sh -c 'mkdir -p /data && tar -C /data -xzf -' < "$src/data.tar.gz"
$COMPOSE up -d --wait
$COMPOSE exec -T scheduler fpl-worker run-once forecast_precompute

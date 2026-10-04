#!/usr/bin/env bash
# Start/stop a local development PostgreSQL cluster from system binaries (no Docker needed).
# Usage: infra/scripts/dev_postgres.sh start|stop|url   (data in data/dev-pg, port $PGPORT or 55432)
set -euo pipefail
PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1 || true)
# macOS/Homebrew and other layouts: use the bindir of the pg_config on PATH
[ -n "$PGBIN" ] || PGBIN=$(pg_config --bindir)
DATA="$(pwd)/data/dev-pg"; PORT="${PGPORT:-55432}"
export LC_ALL="${LC_ALL:-C}"   # postmaster refuses to start without a valid locale on macOS
as_pg() { if [ "$(id -u)" = "0" ]; then runuser -u postgres -- "$@"; else "$@"; fi; }
case "${1:-}" in
  start)
    if [ ! -d "$DATA/data" ]; then
      mkdir -p "$DATA/sock"; [ "$(id -u)" = "0" ] && chown -R postgres "$DATA"
      as_pg "$PGBIN/initdb" -D "$DATA/data" -U fpl --auth=trust -E UTF8 --locale=C >/dev/null
    fi
    as_pg "$PGBIN/pg_ctl" -D "$DATA/data" -o "-p $PORT -k '$DATA/sock' -c listen_addresses=127.0.0.1" \
      -l "$DATA/pg.log" -w start >/dev/null
    as_pg "$PGBIN/createdb" -h 127.0.0.1 -p "$PORT" -U fpl fpl 2>/dev/null || true
    echo "postgresql+psycopg://fpl@127.0.0.1:$PORT/fpl" ;;
  stop) as_pg "$PGBIN/pg_ctl" -D "$DATA/data" -m fast stop ;;
  url) echo "postgresql+psycopg://fpl@127.0.0.1:$PORT/fpl" ;;
  *) echo "usage: $0 start|stop|url" >&2; exit 2 ;;
esac

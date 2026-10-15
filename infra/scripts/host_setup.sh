#!/usr/bin/env bash
# First public deployment on a fresh Ubuntu 24.04 host: docs/DEPLOYMENT.md "First deployment"
# and "Public deployment", executed in order and idempotently (re-running skips what is done).
#
#   sudo -v   # the script uses sudo for apt, docker and system files
#   FPL_PUBLIC_DOMAIN=fpl.example.com FPL_TLS=you@example.com FPL_GIT_REF=<commit sha> \
#   FPL_PRODUCT_NAME="<product name>" bash host_setup.sh
#
# Optional: FPL_SITE_LOGIN_PASSWORD=<password> adds the outer site login at the TLS proxy
# (HTTP Basic, user "operator"); without it the invite-only accounts are the only gate.
# FPL_REPO_URL (default: the GitHub repository), FPL_ROOT (default /srv/fpl).
#
# What it does: Docker Engine + Compose (official apt repository); the repository at FPL_GIT_REF;
# .env from .env.example with a generated database password; the images built with the
# revision recorded; an operator key and the web proxy's key (`fpl-api create-key`: only the
# hashes go in .env; the operator key is written once to $FPL_SECRETS/operator_key, mode 600,
# and printed nowhere); migrations; the pinned historical data; a snapshot; the serving
# forecast; the whole stack behind Caddy (automatic Let's Encrypt certificates); a daily backup;
# the firewall (22, 80, 443). Secrets never appear in the output.
set -euo pipefail

: "${FPL_PUBLIC_DOMAIN:?the site DNS name, e.g. fpl.example.com}"
: "${FPL_TLS:?the ACME e-mail address for the certificate}"
: "${FPL_GIT_REF:?the commit to deploy (full sha or tag)}"
FPL_PRODUCT_NAME=${FPL_PRODUCT_NAME:-}
FPL_REPO_URL=${FPL_REPO_URL:-https://github.com/adivishall/fpl-copilot.git}
FPL_ROOT=${FPL_ROOT:-/srv/fpl}
FPL_SECRETS=${FPL_SECRETS:-/srv/fpl-secrets}
FPL_BACKUPS=${FPL_BACKUPS:-/srv/fpl-backups}
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.public.yml"

log() { printf '%s host_setup: %s\n' "$(date -u +%FT%TZ)" "$*"; }
docker_() { sudo docker "$@"; }
compose() { (cd "$FPL_ROOT" && sudo docker compose $COMPOSE_FILES "$@"); }
set_env() {  # set_env KEY VALUE — replace or append KEY=VALUE in .env (value taken literally)
  python3 - "$FPL_ROOT/.env" "$1" "$2" <<'PY'
import pathlib, sys
p, key, value = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = p.read_text().splitlines()
out, done = [], False
for line in lines:
    if line.startswith(key + "="):
        out.append(f"{key}={value}"); done = True
    else:
        out.append(line)
if not done:
    out.append(f"{key}={value}")
p.write_text("\n".join(out) + "\n")
PY
}

# ---------------------------------------------------------------- 1. Docker Engine + Compose
if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker Engine and Compose from download.docker.com"
  sudo apt-get update -q
  sudo apt-get install -y -q ca-certificates curl git ufw python3
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -q
  sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  sudo apt-get install -y -q git ufw python3 >/dev/null
fi
docker_ compose version >/dev/null
log "docker $(docker_ version --format '{{.Server.Version}}'), $(docker_ compose version --short)"

# a little swap keeps the image build and the forecast safe on an 8 GB host
if [ "$(awk '/SwapTotal/ {print $2}' /proc/meminfo)" = "0" ] && [ ! -f /swapfile ]; then
  log "adding a 2 GB swap file"
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# ---------------------------------------------------------------- 2. the repository at the release
if [ ! -d "$FPL_ROOT/.git" ]; then
  log "cloning $FPL_REPO_URL into $FPL_ROOT"
  sudo mkdir -p "$FPL_ROOT" && sudo chown "$USER" "$FPL_ROOT"
  git clone -q "$FPL_REPO_URL" "$FPL_ROOT"
fi
git -C "$FPL_ROOT" fetch -q origin
git -C "$FPL_ROOT" checkout -q --detach "$FPL_GIT_REF"
REV=$(git -C "$FPL_ROOT" rev-parse HEAD)
log "checked out $REV"
sudo mkdir -p "$FPL_SECRETS" "$FPL_BACKUPS" && sudo chmod 700 "$FPL_SECRETS" "$FPL_BACKUPS"

# ---------------------------------------------------------------- 3. configuration and secrets
cd "$FPL_ROOT"
if [ ! -f .env ]; then
  log "writing .env from .env.example with a generated database password"
  cp .env.example .env && chmod 600 .env
  PGPASS=$(openssl rand -hex 24)
  set_env POSTGRES_PASSWORD "$PGPASS"
  set_env FPL_DATABASE_URL "postgresql+psycopg://fpl:${PGPASS}@postgres:5432/fpl"
  unset PGPASS
fi
chmod 600 .env
set_env FPL_ENVIRONMENT production
set_env FPL_PUBLIC_DOMAIN "$FPL_PUBLIC_DOMAIN"
set_env FPL_TLS "$FPL_TLS"
set_env FPL_CORS_ORIGINS "[\"https://${FPL_PUBLIC_DOMAIN}\"]"
set_env FPL_SOLVER_WORKERS "$(nproc)"
set_env FPL_PRODUCT_NAME "$FPL_PRODUCT_NAME"
set_env FPL_BUILD_REVISION "$REV"
set_env FPL_IMAGE_TAG "${REV:0:12}"

# the outer site login at the proxy: on with a password, otherwise off (an empty import)
if [ -n "${FPL_SITE_LOGIN_PASSWORD:-}" ]; then
  log "site login: on (HTTP Basic at the proxy, user operator)"
  HASH=$(docker_ run --rm caddy:2.10-alpine caddy hash-password --plaintext "$FPL_SITE_LOGIN_PASSWORD")
  printf 'basic_auth {\n\toperator %s\n}\n' "$HASH" > infra/deployment/caddy/auth.caddy
  unset HASH
else
  log "site login: off (the invite-only accounts are the gate)"
  : > infra/deployment/caddy/auth.caddy
fi
chmod 600 infra/deployment/caddy/auth.caddy

# ---------------------------------------------------------------- 4. images
log "building the images (revision $REV)"
compose build --quiet
log "images: $(docker_ images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep -E "^fpl-(engine|web):${REV:0:12} " | tr '\n' ' ')"

# API keys: generated by the image, hashes into .env, the operator key kept once on disk
if ! grep -q '^FPL_API_KEYS_SHA256=\["' .env; then
  log "generating the operator key and the web proxy's key"
  OPS=$(compose run --rm --no-deps -T api fpl-api create-key | head -1)
  WEB=$(compose run --rm --no-deps -T api fpl-api create-key | head -1)
  python3 - "$OPS" "$WEB" "$FPL_SECRETS/operator_key" <<'PY'
import json, os, pathlib, sys
ops, web, dest = json.loads(sys.argv[1]), json.loads(sys.argv[2]), pathlib.Path(sys.argv[3])
fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(ops["api_key"] + "\n")
p = pathlib.Path(".env")
lines = p.read_text().splitlines()
rep = {"FPL_API_KEYS_SHA256": json.dumps([ops["sha256"]]), "FPL_WEB_API_KEYS_SHA256": json.dumps([web["sha256"]]), "FPL_WEB_API_KEY": web["api_key"]}
out = [f"{k}={rep[k]}" if (k := line.split("=", 1)[0]) in rep else line for line in lines]
p.write_text("\n".join(out) + "\n")
PY
  unset OPS WEB
  sudo chown root:root "$FPL_SECRETS/operator_key" && sudo chmod 600 "$FPL_SECRETS/operator_key"
  log "operator key written to $FPL_SECRETS/operator_key (root, 600); its hash and the web key are in .env"
fi

# ---------------------------------------------------------------- 5. database, data, forecast
log "starting postgres and redis, applying migrations"
compose up -d --wait postgres redis
compose run --rm migrate
if [ ! -f "$FPL_SECRETS/.seeded" ]; then
  log "ingesting the pinned historical seasons (a few minutes)"
  compose run --rm --no-deps scheduler fpl-ingest historical --all
  log "exporting the first snapshot"
  compose run --rm --no-deps scheduler fpl-ingest export-snapshot
  sudo touch "$FPL_SECRETS/.seeded"
fi
log "starting the worker and precomputing the serving forecast"
compose up -d --wait worker
compose exec -T scheduler true 2>/dev/null || compose up -d scheduler
compose exec -T scheduler fpl-worker run-once forecast_precompute
for _ in $(seq 1 60); do
  if compose exec -T worker sh -c 'ls /data/artifacts/serving/*.ready >/dev/null 2>&1'; then break; fi
  sleep 10
done
compose exec -T worker sh -c 'ls /data/artifacts/serving/*.ready' >/dev/null || { log "no serving forecast after 10 minutes: see docker compose logs worker"; exit 1; }

# ---------------------------------------------------------------- 6. the whole stack, behind Caddy
log "starting api, scheduler, web and the TLS proxy"
compose up -d --wait
compose ps

# ---------------------------------------------------------------- 7. backups and firewall
cat <<CRON | sudo tee /etc/cron.d/fpl-backup >/dev/null
# daily backup (docs/DEPLOYMENT.md "Backup and restore"); 14 days kept on the host
15 3 * * * root cd $FPL_ROOT && COMPOSE="docker compose $COMPOSE_FILES" infra/scripts/backup.sh $FPL_BACKUPS >> /var/log/fpl-backup.log 2>&1 && find $FPL_BACKUPS -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
CRON
sudo chmod 644 /etc/cron.d/fpl-backup
if ! sudo ufw status | grep -q "Status: active"; then
  log "enabling the firewall: 22, 80, 443"
  sudo ufw allow OpenSSH >/dev/null && sudo ufw allow 80/tcp >/dev/null && sudo ufw allow 443/tcp >/dev/null && sudo ufw allow 443/udp >/dev/null
  sudo ufw --force enable >/dev/null
fi

log "done. listening sockets:"
sudo ss -ltnp | awk 'NR==1 || /:(80|443|8000|3000|5432|6379|9101|9090) /'
log "https://$FPL_PUBLIC_DOMAIN/ answers $(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "https://$FPL_PUBLIC_DOMAIN/" || echo 'not yet (certificate issuance can take a minute)')"

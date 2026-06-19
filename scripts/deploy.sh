#!/usr/bin/env bash
# One-command deploy for the Universal Mail Automation service (the FastAPI app:
# API at /v1/*, dashboard at /app, health at /health).
#
# Usage:
#   scripts/deploy.sh [target]
#
# Targets:
#   docker      (default) Build the image and run it locally on $PORT, then smoke-test it.
#   image                 Build the image and push it to a registry ($IMAGE).
#   cloudflare            Deploy the read-only demo Worker (needs CLOUDFLARE_API_TOKEN).
#   render                Trigger a Render deploy ($RENDER_DEPLOY_HOOK) or print blueprint steps.
#   help                  Show this help.
#
# Environment:
#   PORT                  Local port for `docker` (default 8000).
#   IMAGE                 Image ref for build/push (default: mail-api:local; for `image`
#                         set e.g. ghcr.io/<owner>/<repo>:latest).
#   ENV_FILE              Path to a runtime env file passed to the container (default: prod.env if present).
#   RENDER_DEPLOY_HOOK    Render deploy hook URL for the `render` target.
#
# This wraps the same Dockerfile the CI Deploy workflow publishes to GHCR, so a
# local `scripts/deploy.sh` and a CI/registry deploy produce the same artifact.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

TARGET="${1:-docker}"
PORT="${PORT:-8000}"
IMAGE="${IMAGE:-mail-api:local}"
CONTAINER_NAME="mail-api"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "required tool not found: $1"; }

usage() {
  sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# Resolve the env-file flag for `docker run`. Optional: the pure endpoints
# (/health, /v1/senders/check, /v1/billing/plans, /app) need no credentials.
env_file_args() {
  local f="${ENV_FILE:-}"
  if [ -z "$f" ] && [ -f "$REPO_DIR/prod.env" ]; then f="$REPO_DIR/prod.env"; fi
  if [ -n "$f" ]; then
    [ -f "$f" ] || die "ENV_FILE not found: $f"
    printf -- '--env-file\n%s\n' "$f"
  fi
}

# Poll /health until the container answers (or time out).
wait_for_health() {
  local url="$1"
  for _ in $(seq 1 30); do
    if curl -fsS "$url" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

# Hit the credential-free endpoints to prove the deploy actually serves.
smoke_test() {
  local base="$1"
  log "Smoke-testing $base"
  curl -fsS "$base/health" | grep -q '"status":"ok"' || die "health check failed"
  curl -fsS "$base/v1/billing/plans" | grep -q '"plans"' || die "billing/plans failed"
  curl -fsS -H 'content-type: application/json' \
    -d '{"sender":"clerk@courts.ca.gov"}' \
    "$base/v1/senders/check" | grep -q '"protected":true' || die "protected-sender gate failed"
  curl -fsS "$base/app/" >/dev/null || die "dashboard (/app) failed"
  log "Smoke test passed."
}

deploy_docker() {
  need docker
  need curl
  log "Building image $IMAGE"
  docker build -t "$IMAGE" "$REPO_DIR"

  log "(Re)starting container $CONTAINER_NAME on port $PORT"
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

  local args=()
  while IFS= read -r line; do [ -n "$line" ] && args+=("$line"); done < <(env_file_args)

  # Guard the array expansion: macOS bash 3.2 treats "${args[@]}" on an empty
  # array as an unbound variable under `set -u`.
  docker run -d --name "$CONTAINER_NAME" \
    -p "$PORT:8000" -e "PORT=8000" ${args[@]+"${args[@]}"} "$IMAGE" >/dev/null

  local base="http://127.0.0.1:$PORT"
  log "Waiting for the app to become healthy..."
  if ! wait_for_health "$base/health"; then
    docker logs "$CONTAINER_NAME" || true
    die "container did not become healthy"
  fi
  smoke_test "$base"
  log "Deployed. Dashboard: $base/app  |  Health: $base/health"
  log "Stop with: docker rm -f $CONTAINER_NAME"
}

deploy_image() {
  need docker
  case "$IMAGE" in
    */*) ;;  # looks like a registry ref
    *) die "set IMAGE to a registry ref, e.g. IMAGE=ghcr.io/<owner>/<repo>:latest scripts/deploy.sh image" ;;
  esac
  log "Building image $IMAGE"
  docker build -t "$IMAGE" "$REPO_DIR"
  log "Pushing $IMAGE (ensure you are logged in: docker login <registry>)"
  docker push "$IMAGE"
  log "Pushed. Run anywhere: docker run -p 8000:8000 --env-file prod.env $IMAGE"
}

deploy_cloudflare() {
  need npx
  [ -n "${CLOUDFLARE_API_TOKEN:-}" ] || warn "CLOUDFLARE_API_TOKEN not set; wrangler may prompt for login."
  log "Deploying the demo Worker with wrangler"
  npx --yes wrangler@4 deploy
  log "Deployed Worker (see wrangler.toml for the route/domain)."
}

deploy_render() {
  if [ -n "${RENDER_DEPLOY_HOOK:-}" ]; then
    need curl
    log "Triggering Render deploy via deploy hook"
    curl -fsS -X POST "$RENDER_DEPLOY_HOOK" >/dev/null
    log "Render deploy triggered. Watch progress in the Render dashboard."
  else
    cat <<'EOF'
Render uses the committed blueprint (render.yaml). One-time setup:
  1. Push this repo to GitHub.
  2. In Render: New + → Blueprint → pick this repo (provisions a Docker web
     service with a /health check).
  3. Set provider credentials as env vars in the dashboard (see DEPLOY.md).
After that, pushes to main auto-deploy. To trigger from this script, set
RENDER_DEPLOY_HOOK to the service's deploy-hook URL and re-run:
  RENDER_DEPLOY_HOOK="https://api.render.com/deploy/srv-...?key=..." scripts/deploy.sh render
EOF
  fi
}

case "$TARGET" in
  docker)     deploy_docker ;;
  image)      deploy_image ;;
  cloudflare) deploy_cloudflare ;;
  render)     deploy_render ;;
  help|-h|--help) usage ;;
  *) die "unknown target: $TARGET (try: docker | image | cloudflare | render | help)" ;;
esac

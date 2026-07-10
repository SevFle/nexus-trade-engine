#!/usr/bin/env bash
#
# Blue/green deployment for the Nexus Trade Engine API image.
#
# Flow (zero-downtime, health-gated):
#   1. validate the target slot (blue|green) and resolve its sibling slot
#   2. build + tag the image as nexus-trade:<slot>
#   3. start a replacement container on the slot's mapped host port
#   4. poll the /health endpoint until it answers 200 (readiness gate)
#   5. on success: flip traffic to the new slot (state file + optional
#      nginx upstream reload) and gracefully stop the previous slot
#      on failure: abort — stop the failed new container and LEAVE the
#      previous slot running so traffic is uninterrupted
#
# The script is written so every side-effecting command is overridable via
# an env var (DOCKER_CMD, NGINX_RELOAD_CMD, HEALTH_PROBE_CMD, ...). That
# keeps the helper functions unit-testable (see tests/test_deploy.py) and
# lets operators point at a remote docker context / custom probe.
#
# Usage:
#   scripts/ops/deploy.sh blue
#   scripts/ops/deploy.sh green
#   SLOT=green scripts/ops/deploy.sh
#   DRY_RUN=1 scripts/ops/deploy.sh blue      # print actions, run nothing
#
# Env knobs (all optional):
#   SLOT                     target slot (blue|green); positional arg wins
#   BLUE_PORT / GREEN_PORT   host ports            (default 8001 / 8002)
#   INTERNAL_PORT            container listen port (default 8000)
#   IMAGE_NAME               image base name       (default nexus-trade)
#   CONTAINER_BASE           container name prefix (default nexus-trade)
#   BUILD_CONTEXT            docker build context  (default .)
#   DOCKERFILE               dockerfile path       (default Dockerfile)
#   ENV_FILE                 --env-file passed to run (default .env)
#   DOCKER_NETWORK           --network for run     (default: none)
#   DOCKER_RUN_EXTRA         extra `docker run` flags (default: none)
#   DOCKER_CMD               docker binary         (default docker)
#   HEALTH_HOST              probe host            (default 127.0.0.1)
#   HEALTH_PATH              probe path            (default /health)
#   HEALTH_TIMEOUT_SEC       per-probe curl -m     (default 3)
#   HEALTH_PROBE_CMD         override the probe entirely (tests)
#   READINESS_TIMEOUT_SEC    overall wait budget   (default 90)
#   READINESS_INTERVAL_SEC   poll cadence          (default 2)
#   ACTIVE_SLOT_FILE         where the active slot is recorded
#                            (default /var/run/nexus-active-slot)
#   NGINX_UPSTREAM_CONF      regenerate this upstream file on swap
#                            (default: none)
#   NGINX_UPSTREAM_NAME      upstream block name    (default nexus_backend)
#   NGINX_RELOAD_CMD         reload command         (default nginx -s reload)
#   GRACEFUL_STOP_TIMEOUT    `docker stop -t` grace (default 30)
#   DRY_RUN                  1 = log commands, do not build/run/stop
#
# Exit codes:
#   0  deploy succeeded, traffic switched
#   1  build/start/swap failed (old slot left running when possible)
#   2  invalid usage (bad slot / missing args)
#
# Reference: docs/deployment.md  (Rollout process / Rollback sections)

set -euo pipefail

# ---------------------------------------------------------------------------
# logging helpers — everything goes to stderr so a function's real return
# value (printed to stdout) is never polluted by log noise.
# ---------------------------------------------------------------------------
log() {
  printf '[deploy] %s\n' "$*" >&2
}

die() {
  printf '[deploy] ERROR: %s\n' "$*" >&2
  exit "${2:-1}"
}

# ---------------------------------------------------------------------------
# pure helpers — no side effects, fully unit-testable by sourcing this file.
# ---------------------------------------------------------------------------

# validate_slot <slot> → exit 0 if blue|green, exit 1 otherwise.
validate_slot() {
  case "${1:-}" in
    blue|green) return 0 ;;
    *)          return 1 ;;
  esac
}

# opposite_slot <slot> → print the sibling slot on stdout.
opposite_slot() {
  case "$1" in
    blue)  printf 'green' ;;
    green) printf 'blue' ;;
    *)     return 1 ;;
  esac
}

# port_for_slot <slot> → print the mapped host port on stdout.
port_for_slot() {
  case "$1" in
    blue)  printf '%s' "${BLUE_PORT:-8001}" ;;
    green) printf '%s' "${GREEN_PORT:-8002}" ;;
    *)     return 1 ;;
  esac
}

# container_name <slot> → print the container name on stdout.
container_name() {
  printf '%s-%s' "${CONTAINER_BASE:-nexus-trade}" "$1"
}

# image_tag <slot> → print the image tag on stdout.
image_tag() {
  printf '%s:%s' "${IMAGE_NAME:-nexus-trade}" "$1"
}

# ---------------------------------------------------------------------------
# health probing
# ---------------------------------------------------------------------------

# probe_once <host> <port> [path] → 0 if healthy (HTTP 2xx), non-zero otherwise.
# A single probe — no retry. Overridable via HEALTH_PROBE_CMD for tests.
probe_once() {
  local host="$1" port="$2" path="${3:-${HEALTH_PATH:-/health}}"
  if [ -n "${HEALTH_PROBE_CMD:-}" ]; then
    # shellcheck disable=SC2086
    $HEALTH_PROBE_CMD "$host" "$port" "$path"
    return $?
  fi
  curl \
    --fail \
    --silent \
    --show-error \
    --no-progress-meter \
    --max-time "${HEALTH_TIMEOUT_SEC:-3}" \
    "http://${host}:${port}${path}" -o /dev/null
}

# retry_until_ok <timeout_sec> <interval_sec> <cmd...>
#   Poll <cmd> every <interval_sec> until it exits 0 or <timeout_sec> elapses.
#   Prints the number of attempts made to stdout.
#   Returns 0 on success, 1 on timeout. Pure control flow → unit-testable.
retry_until_ok() {
  local timeout_sec="$1"
  local interval_sec="$2"
  shift 2
  [ "$#" -gt 0 ] || return 1

  local now deadline attempt
  now=$(date +%s)
  deadline=$(( now + timeout_sec ))
  attempt=0
  while true; do
    attempt=$(( attempt + 1 ))
    if "$@"; then
      printf '%s' "$attempt"
      return 0
    fi
    # Check the deadline *after* a failed probe so a slow-but-successful
    # first probe is never skipped, and so timeout=0 still gets one attempt.
    if [ "$(date +%s)" -ge "$deadline" ]; then
      printf '%s' "$attempt"
      return 1
    fi
    sleep "$interval_sec"
  done
}

# wait_for_health <slot> → poll the slot's mapped port until healthy or the
# readiness budget is exhausted. Returns 0 healthy, 1 timed out.
wait_for_health() {
  local slot="$1"
  local port host path attempts
  port=$(port_for_slot "$slot")
  host="${HEALTH_HOST:-127.0.0.1}"
  path="${HEALTH_PATH:-/health}"
  attempts=$(retry_until_ok \
    "${READINESS_TIMEOUT_SEC:-90}" \
    "${READINESS_INTERVAL_SEC:-2}" \
    probe_once "$host" "$port" "$path")
  local rc=$?
  if [ "$rc" -eq 0 ]; then
    log "$slot healthy after ${attempts} attempt(s) on http://${host}:${port}${path}"
  else
    log "$slot did not become healthy within ${READINESS_TIMEOUT_SEC:-90}s (${attempts} attempt(s))"
  fi
  return "$rc"
}

# ---------------------------------------------------------------------------
# docker side-effects — every binary is overridable so tests can mock them.
# ---------------------------------------------------------------------------

is_dry_run() {
  [ "${DRY_RUN:-0}" = "1" ]
}

# build_image <slot>
build_image() {
  local slot="$1" tag
  tag=$(image_tag "$slot")
  local ctx="${BUILD_CONTEXT:-.}" file="${DOCKERFILE:-Dockerfile}"
  log "building image ${tag} (context=${ctx} file=${file})"
  if is_dry_run; then
    log "DRY_RUN: ${DOCKER_CMD:-docker} build -t ${tag} -f ${file} ${ctx}"
    return 0
  fi
  ${DOCKER_CMD:-docker} build -t "$tag" -f "$file" "$ctx"
}

# start_container <slot> → run the replacement container detached on its port.
start_container() {
  local slot="$1" name port tag internal
  name=$(container_name "$slot")
  port=$(port_for_slot "$slot")
  tag=$(image_tag "$slot")
  internal="${INTERNAL_PORT:-8000}"

  local -a run_args=(
    run -d
    --name "$name"
    --restart unless-stopped
    -p "127.0.0.1:${port}:${internal}"
  )
  [ -n "${DOCKER_NETWORK:-}" ] && run_args+=(--network "$DOCKER_NETWORK")
  [ -f "${ENV_FILE:-.env}" ] && run_args+=(--env-file "${ENV_FILE:-.env}")
  # Deliberately unquoted: DOCKER_RUN_EXTRA is a free-form flag string.
  # shellcheck disable=SC2086
  if is_dry_run; then
    log "DRY_RUN: ${DOCKER_CMD:-docker} ${run_args[*]} ${DOCKER_RUN_EXTRA:-} ${tag}"
    return 0
  fi
  ${DOCKER_CMD:-docker} "${run_args[@]}" ${DOCKER_RUN_EXTRA:-} "$tag"
}

# stop_slot <slot> → graceful stop + remove of a slot's container.
# Tolerates a missing container (e.g. first-ever deploy of a slot).
stop_slot() {
  local slot="$1" name grace
  name=$(container_name "$slot")
  grace="${GRACEFUL_STOP_TIMEOUT:-30}"
  if is_dry_run; then
    log "DRY_RUN: stop+rm ${name}"
    return 0
  fi
  log "stopping ${name} (grace ${grace}s)"
  ${DOCKER_CMD:-docker} stop -t "$grace" "$name" >/dev/null 2>&1 || true
  ${DOCKER_CMD:-docker} rm -f "$name" >/dev/null 2>&1 || true
}

# rollback_new <slot> → the rollback path. Stops ONLY the just-started new
# slot; the previous slot is never touched so it keeps serving traffic.
rollback_new() {
  local slot="$1"
  log "rollback: removing failed ${slot} container; previous slot left intact"
  stop_slot "$slot"
}

# ---------------------------------------------------------------------------
# traffic switch
# ---------------------------------------------------------------------------

# swap_traffic <slot> → atomically point traffic at <slot>:
#   • write the active slot to ACTIVE_SLOT_FILE (the source of truth the LB /
#     nginx sidecar reads), then
#   • optionally regenerate NGINX_UPSTREAM_CONF and reload nginx.
# Returns 0 on success, 1 if the reload fails (caller decides to roll back).
swap_traffic() {
  local slot="$1" port state_file
  port=$(port_for_slot "$slot")
  state_file="${ACTIVE_SLOT_FILE:-/var/run/nexus-active-slot}"

  log "switching active slot -> ${slot} (port ${port})"
  if ! is_dry_run; then
    ( umask 022 && printf '%s\n' "$slot" > "$state_file" ) 2>/dev/null \
      || log "warn: could not write ${state_file} (continuing)"
  fi

  local conf="${NGINX_UPSTREAM_CONF:-}"
  if [ -n "$conf" ]; then
    local upstream="${NGINX_UPSTREAM_NAME:-nexus_backend}"
    if is_dry_run; then
      log "DRY_RUN: rewrite ${conf} -> server 127.0.0.1:${port}; reload"
      return 0
    fi
    local tmp
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' RETURN
    {
      printf '# managed by scripts/ops/deploy.sh — do not edit by hand\n'
      printf 'upstream %s {\n' "$upstream"
      printf '    server 127.0.0.1:%s;\n' "$port"
      printf '}\n'
    } > "$tmp"
    # mv is atomic on the same filesystem; nginx never reads a half-written file.
    if ! mv -f "$tmp" "$conf" 2>/dev/null; then
      rm -f "$tmp"
      log "ERROR: could not install ${conf}"
      return 1
    fi
    # shellcheck disable=SC2086
    if ! ${NGINX_RELOAD_CMD:-nginx -s reload}; then
      log "ERROR: nginx reload failed"
      return 1
    fi
  fi
  return 0
}

# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

# deploy <slot> → full blue/green rollout with health gating + rollback.
deploy() {
  local slot="$1"
  validate_slot "$slot" || { die "invalid slot '${slot:-}' (expected blue|green)" 2; }

  local prev new_port
  prev=$(opposite_slot "$slot")
  new_port=$(port_for_slot "$slot")
  log "blue/green deploy -> ${slot} (host port ${new_port}); previous=${prev}"

  if ! build_image "$slot"; then
    die "image build failed for ${slot}; previous slot ${prev} untouched" 1
  fi

  if ! start_container "$slot"; then
    die "failed to start ${slot} container; previous slot ${prev} untouched" 1
  fi

  if ! wait_for_health "$slot"; then
    rollback_new "$slot"
    die "${slot} failed readiness — rolled back; ${prev} still serving" 1
  fi

  if ! swap_traffic "$slot"; then
    rollback_new "$slot"
    die "traffic swap failed — rolled back; ${prev} still serving" 1
  fi

  log "draining previous slot ${prev}"
  stop_slot "$prev"

  log "deploy complete — active slot: ${slot} (port ${new_port})"
}

usage() {
  cat >&2 <<'EOF'
Usage: deploy.sh <blue|green>

Blue/green deploy of the Nexus API image with health-check gating.

  SLOT=green ./deploy.sh        set target via env
  DRY_RUN=1 ./deploy.sh blue    preview actions without building/running

See the script header for the full list of env knobs.
EOF
}

main() {
  local slot="${1:-${SLOT:-}}"
  if [ -z "$slot" ]; then
    usage
    die "target slot is required (blue|green)" 2
  fi
  if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
  fi
  deploy "$slot"
}

# Only run main when executed directly, not when sourced (for unit tests).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi

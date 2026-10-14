#!/bin/bash
STEALTHBOX_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Ports: the environment wins, then .env (what docker compose uses), then the defaults.
env_setting() {
  [ -f "${STEALTHBOX_ROOT}/.env" ] || return 0
  sed -n "s/^[[:space:]]*$1=\([0-9][0-9]*\)[[:space:]]*$/\1/p" "${STEALTHBOX_ROOT}/.env" | tail -n 1
}
CDP_PORT="${STEALTHBOX_CDP_PORT:-$(env_setting STEALTHBOX_CDP_PORT)}"
CDP_PORT="${CDP_PORT:-9222}"
VIEWER_PORT="${STEALTHBOX_VIEWER_PORT:-$(env_setting STEALTHBOX_VIEWER_PORT)}"
VIEWER_PORT="${VIEWER_PORT:-6080}"
API_PORT="${STEALTHBOX_API_PORT:-$(env_setting STEALTHBOX_API_PORT)}"
API_PORT="${API_PORT:-8423}"
CDP_URL="http://127.0.0.1:${CDP_PORT}"
VIEWER_URL="http://127.0.0.1:${VIEWER_PORT}"
API_URL="http://127.0.0.1:${API_PORT}"
STATE_DIR="${HOME}/.stealthbox"
DATA_DIR="${STEALTHBOX_DATA_DIR:-${STATE_DIR}/data}"

# OrbStack's docker CLI lives outside the default PATH.
for bin_dir in "${HOME}/.orbstack/bin"; do
  case ":${PATH}:" in
    *":${bin_dir}:"*) ;;
    *) export PATH="${bin_dir}:${PATH}" ;;
  esac
done

# macOS only: OrbStack's Linux machines can see the Mac's /Applications too.
orbstack_app() {
  [ "$(uname -s)" = Darwin ] && { [ -d /Applications/OrbStack.app ] || [ -d "${HOME}/Applications/OrbStack.app" ]; }
}

# With OrbStack installed, Stealthbox uses its engine for its own commands only; the
# user's default Docker context (Docker Desktop, Colima...) is left alone.
if [ -z "${DOCKER_CONTEXT:-}" ] && [ -z "${DOCKER_HOST:-}" ] && orbstack_app; then
  export DOCKER_CONTEXT=orbstack
fi

health() {
  docker inspect -f '{{.State.Health.Status}}' stealthbox 2>/dev/null || echo not_running
}

# The share tunnel runs in the container, which writes its link here; a container
# that is not running has no tunnel, whatever the file says.
active_share_url() {
  [ "$(health)" = healthy ] || return 0
  # cloudflared in the share service: its address once the edge can route it.
  host="$(docker exec stealthbox sh -c 'curl -fsS -m 2 http://share:20241/ready >/dev/null && curl -fsS -m 2 http://share:20241/quicktunnel' 2>/dev/null \
    | sed -n 's/.*"hostname":"\([^"]*\)".*/\1/p')"
  [ -z "$host" ] || echo "https://${host}/console"
}

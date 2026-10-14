#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

# --force-onboard: show the first-run next steps even when the OpenRouter key is set.
force_onboard=no
for argument in "$@"; do
  case "$argument" in
    --force-onboard) force_onboard=yes ;;
    *) echo "Unknown option: $argument (start takes --force-onboard)" >&2; exit 2 ;;
  esac
done

bold=$'\033[1m'; green=$'\033[32m'; red=$'\033[31m'; reset=$'\033[0m'

fail() {
  echo
  echo "${red}${bold}Problem:${reset} $1"
  echo
  exit 1
}

echo "${bold}Stealthbox${reset} - starting the browser"
echo

source ./lib.sh

open_orbstack() {
  if [ -d /Applications/OrbStack.app ]; then
    open /Applications/OrbStack.app || echo "Waiting for OrbStack to finish starting..."
    return
  fi
  open "${HOME}/Applications/OrbStack.app" || echo "Waiting for OrbStack to finish starting..."
}

dotenv_defines_tz() {
  [ -f .env ] || return 1
  grep -qE '^[[:space:]]*TZ=' .env
}

detect_host_timezone() {
  tz=""
  if command -v timedatectl >/dev/null 2>&1; then
    tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  fi
  case "$tz" in
    ""|n/a) ;;
    *) printf '%s\n' "$tz"; return 0 ;;
  esac
  if [ -f /etc/timezone ]; then
    tz="$(tr -d '[:space:]' < /etc/timezone)"
    if [ -n "$tz" ]; then
      printf '%s\n' "$tz"
      return 0
    fi
  fi
  if [ -L /etc/localtime ]; then
    link="$(readlink /etc/localtime)"
    case "$link" in
      *zoneinfo/*)
        printf '%s\n' "${link#*zoneinfo/}"
        return 0
        ;;
    esac
  fi
  return 1
}

timezone_name_ok() {
  case "$1" in
    ""|*..*|*[!A-Za-z0-9_+/-]*) return 1 ;;
  esac
}

export_host_timezone() {
  if [ -n "${TZ:-}" ]; then
    return 0
  fi
  if dotenv_defines_tz; then
    return 0
  fi
  tz="$(detect_host_timezone || true)"
  if ! timezone_name_ok "$tz"; then
    return 0
  fi
  export TZ="$tz"
}

if orbstack_app; then
  if ! docker info >/dev/null 2>&1; then
    echo "Starting OrbStack..."
    open_orbstack
    orb_bin=""
    if [ -x /Applications/OrbStack.app/Contents/MacOS/bin/orb ]; then
      orb_bin="/Applications/OrbStack.app/Contents/MacOS/bin/orb"
    elif [ -x "${HOME}/Applications/OrbStack.app/Contents/MacOS/bin/orb" ]; then
      orb_bin="${HOME}/Applications/OrbStack.app/Contents/MacOS/bin/orb"
    fi
    if [ -n "$orb_bin" ]; then
      "$orb_bin" start >/dev/null 2>&1 || echo "Waiting for OrbStack to finish starting..."
    fi
    ready=""
    for _ in $(seq 1 120); do
      if docker info >/dev/null 2>&1; then
        ready=yes
        break
      fi
      sleep 2
    done
    if [ -z "${ready}" ]; then
      fail "OrbStack did not become ready.
Open OrbStack from Applications and finish its setup, then run this again.
  https://orbstack.dev/download"
    fi
  fi
  echo "Using OrbStack."
elif ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  if [ "$(uname)" = "Darwin" ]; then
    fail "OrbStack is not installed, and no container engine is running.
On a Mac, install Stealthbox with one command:
  gh api repos/jonclegg/stealthbox/contents/install.sh -H 'Accept: application/vnd.github.raw' | sh
Or install OrbStack, open it, then run this again:
  https://orbstack.dev/download"
  fi
  fail "Docker Engine is not available.
Install Docker Engine, then make sure 'docker info' works for this user:
  https://docs.docker.com/engine/install/
If Docker is installed but 'docker info' fails, start it and join the docker group, then log in again:
  sudo systemctl enable --now docker
  sudo usermod -aG docker \"\$USER\""
fi

if ! docker compose version >/dev/null 2>&1; then
  if [ "$(uname)" = "Darwin" ]; then
    fail "This container engine is missing 'docker compose'. Update OrbStack or Docker Desktop (with Colima, set up Homebrew's docker-compose plugin), then run this again."
  fi
  fail "This container engine is missing 'docker compose'.
Install the Docker Compose plugin and run this again.
  https://docs.docker.com/compose/install/linux/"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env with default settings (edit it later to add a proxy)."
fi

export_host_timezone

# The API writes notes, run logs and photos here. Create it as this user, so on
# Linux the bind mount is ours and the API runs as us rather than root.
mkdir -p "${STEALTHBOX_DATA_DIR:-${HOME}/.stealthbox/data}"

# On a Mac, present the browser as a Mac (docker-compose.mac.yml). Compose reads
# COMPOSE_FILE from .env, so every later compose command picks it up too.
if [ "$(uname -s)" = "Darwin" ] && ! grep -qE '^COMPOSE_FILE=' .env; then
  printf '\n# Set by start.sh on a Mac: macOS persona, Retina screen, and Mac fonts.\nCOMPOSE_FILE=docker-compose.yml:docker-compose.mac.yml\n' >> .env
fi

echo "1/3 Building Stealthbox (the first time takes several minutes)..."
# The browser core release is private: the build reads it with GITHUB_TOKEN,
# by default the signed-in gh's.
if [ -z "${GITHUB_TOKEN:-}" ] && command -v gh >/dev/null 2>&1; then
  GITHUB_TOKEN="$(gh auth token 2>/dev/null || true)"
  export GITHUB_TOKEN
fi
[ -n "${GITHUB_TOKEN:-}" ] || fail "Building Stealthbox needs GITHUB_TOKEN, or gh signed in (gh auth login), to read the private browser core release."
docker compose build --quiet stealthbox

echo "2/3 Starting the browser..."
if ! up_log="$(docker compose up -d --force-recreate stealthbox 2>&1)"; then
  echo "$up_log"
  case "$up_log" in
    *"port is already allocated"*|*"address already in use"*|*"ports are not available"*)
      fail "Another program on this computer is already using port ${CDP_PORT}, ${VIEWER_PORT} or ${API_PORT}.
Pick free ports in ${PWD}/.env, for example:
  STEALTHBOX_CDP_PORT=9333
  STEALTHBOX_VIEWER_PORT=6090
  STEALTHBOX_API_PORT=8433
then run: stealthbox start"
      ;;
  esac
  fail "Docker could not start the browser. See the message above."
fi

# The first start also downloads the GeoIP database (~70 MB).
echo "3/3 Waiting for the browser to be ready..."
for _ in $(seq 1 300); do
  status="$(docker inspect -f '{{.State.Health.Status}}' stealthbox 2>/dev/null || echo missing)"
  [ "$status" = "healthy" ] && break
  sleep 2
done

if [ "$status" != "healthy" ]; then
  docker compose logs --tail 40 stealthbox
  fail "The browser did not become ready (status: $status). See the log above, or skills/stealthbox/SKILL.md > Troubleshooting."
fi

viewer_url="http://$(docker compose port stealthbox 6080)"
cdp_url="http://$(docker compose port stealthbox 9222)"
api_url="http://$(docker compose port stealthbox 8423)"

cat <<EOF

${green}${bold}You're ready!${reset}

  See the browser:    ${viewer_url}
  Agents and scripts: ${cdp_url}   (Playwright, Puppeteer, or open this folder in your agent)
  Chat with it:       stealthbox chat "<message>"   (API ${api_url})
  Watch from a phone: stealthbox share
  Stop everything:    stealthbox stop

  Only this computer can connect. Your logins are kept between restarts.

EOF

# Stealthbox's own agent needs an OpenRouter key. Ask for it on a local page, never
# in a terminal an agent might be reading or in chat.
if [ "$force_onboard" = yes ] || [ ! -s "${STEALTHBOX_DATA_DIR:-${HOME}/.stealthbox/data}/openrouter.key" ]; then
  cat <<EOF
  Next: stealthbox key     paste your OpenRouter key into the page it opens
        stealthbox smoke example.com   check it can reach sites you use

EOF
fi

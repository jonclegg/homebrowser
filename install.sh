#!/bin/sh
# One-step install or update for macOS and Linux:
#   curl -fsSL https://raw.githubusercontent.com/jonclegg/stealthbox/main/install.sh | sh
#
# POSIX sh (runs under dash, BusyBox ash, bash and zsh). Everything is inside main(), so a
# partially downloaded script never runs. The app lives in ~/.stealthbox/app and is replaced
# wholesale from a GitHub tarball on every run: local edits never block an update, and the
# previous copy is kept in ~/.stealthbox/app.previous. Your .env settings carry over.
#
# Optional: STEALTHBOX_TARBALL_URL installs from a tarball at that URL instead.
# Arguments go to `stealthbox start`: `| sh -s -- --force-onboard` shows the first-run
# next steps even when the OpenRouter key is set.
#
# For agents running this for a user: without a container engine the installer
# changes nothing and exits 3 after a line starting "STEALTHBOX_NEEDS: docker".
# Ask the user whether to install one, and if they agree, run it again with
# STEALTHBOX_INSTALL_DOCKER=yes. Exit 4 ("STEALTHBOX_NEEDS: user") means a step
# only the user can do (a sudo password, a log out and in); it says which.
set -eu

TARBALL_URL="${STEALTHBOX_TARBALL_URL:-https://github.com/jonclegg/stealthbox/archive/refs/heads/main.tar.gz}"
STATE_DIR="${HOME:-}/.stealthbox"
INSTALL_DIR="${STATE_DIR}/app"
ORBSTACK_DOWNLOAD="https://orbstack.dev/download"
ORBSTACK_MIN_MACOS=14

fail() {
  printf '\nProblem: %s\n\n' "$1" >&2
  exit 1
}

# No container engine and no consent to install one: change nothing, say what
# would be installed, and let the agent ask the user.
needs_docker() {
  printf '\nSTEALTHBOX_NEEDS: docker\n%s\n\nTo install it, run this installer again with STEALTHBOX_INSTALL_DOCKER=yes:\n  curl -fsSL https://raw.githubusercontent.com/jonclegg/stealthbox/main/install.sh | STEALTHBOX_INSTALL_DOCKER=yes sh\n\n' "$1" >&2
  exit 3
}

# A step only the user can do.
needs_user() {
  printf '\nSTEALTHBOX_NEEDS: user\n%s\n\n' "$1" >&2
  exit 4
}

install_docker_consented() {
  [ "${STEALTHBOX_INSTALL_DOCKER:-}" = yes ]
}

# sudo that never hangs on a password prompt nobody can see.
can_sudo() {
  [ "$(id -u)" = 0 ] && return 0
  command -v sudo >/dev/null 2>&1 || return 1
  sudo -n true 2>/dev/null && return 0
  # A terminal the user can type a password into (none when an agent runs this).
  # shellcheck disable=SC2024  # the redirect is the point: sudo prompts on the tty
  [ -r /dev/tty ] && sudo -v </dev/tty 2>/dev/null
}

as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}

say() {
  printf '%s\n' "$1"
}

need() {
  command -v "$1" >/dev/null 2>&1 || fail "This installer needs '$1', which is not installed. $2"
}

preflight() {
  [ -n "${HOME:-}" ] || fail "HOME is not set."
  if [ "$(id -u)" = 0 ] && [ -n "${SUDO_USER:-}" ]; then
    fail "Run the installer as yourself, not with sudo:
  curl -fsSL https://raw.githubusercontent.com/jonclegg/stealthbox/main/install.sh | sh"
  fi
  need curl "Install curl, then run this again."
  need tar "Install tar, then run this again."
  need mktemp "Install coreutils, then run this again."
}

# ---------------------------------------------------------------- macOS

orbstack_installed() {
  [ "$(uname -s)" = Darwin ] && { [ -d /Applications/OrbStack.app ] || [ -d "${HOME}/Applications/OrbStack.app" ]; }
}

have_brew() {
  if command -v brew >/dev/null 2>&1; then
    return 0
  fi
  for prefix in /opt/homebrew /usr/local; do
    if [ -x "${prefix}/bin/brew" ]; then
      PATH="${prefix}/bin:${PATH}"
      export PATH
      return 0
    fi
  done
  return 1
}

macos_major() {
  sw_vers -productVersion | cut -d. -f1
}

# podman-docker answers to 'docker' but can't run Stealthbox's compose stack as-is.
docker_is_podman() {
  docker --version 2>/dev/null | grep -qi podman
}

# A Docker engine that already answers (Docker Desktop, Colima, Rancher Desktop...).
docker_engine_running() {
  command -v docker >/dev/null 2>&1 && ! docker_is_podman && docker info >/dev/null 2>&1
}

wait_for_orbstack_app() {
  say "Waiting for OrbStack to appear in Applications..."
  i=0
  while [ "$i" -lt 180 ]; do
    if orbstack_installed; then
      say "OrbStack found."
      return 0
    fi
    i=$((i + 1))
    sleep 2
  done
  fail "OrbStack was not found. Download it from ${ORBSTACK_DOWNLOAD}, open the app, then run this installer again."
}

ensure_mac_engine() {
  if orbstack_installed; then
    say "Container engine: OrbStack."
    return 0
  fi
  if docker_engine_running; then
    docker compose version >/dev/null 2>&1 || fail "Your Docker engine is running, but 'docker compose' is missing.
Docker Desktop includes it (update Docker Desktop). With Homebrew's docker-compose, follow the
'cliPluginsExtraDirs' note that 'brew info docker-compose' prints. Check with 'docker compose version',
then run this installer again."
    say "Container engine: the Docker engine already running on this Mac."
    return 0
  fi
  major="$(macos_major)"
  if [ "$major" -lt "$ORBSTACK_MIN_MACOS" ]; then
    fail "This Mac runs macOS ${major}, and OrbStack needs macOS ${ORBSTACK_MIN_MACOS} or newer.
Install a Docker engine that supports your macOS, start it, then run this installer again:
  Colima (free):   brew install colima docker docker-compose && colima start
  Docker Desktop:  https://docs.docker.com/desktop/setup/install/mac-install/
Make sure 'docker info' and 'docker compose version' both work."
  fi
  install_docker_consented || needs_docker "Stealthbox runs its browser in a container, and this Mac has no container engine.
It would install OrbStack, a lightweight container engine for macOS (free for personal use):
  https://orbstack.dev"
  say "Stealthbox runs its browser in a container. Installing OrbStack, a lightweight container engine for macOS."
  if have_brew; then
    say "Installing OrbStack with Homebrew..."
    brew install --cask orbstack </dev/null
    if orbstack_installed; then
      return 0
    fi
  fi
  cat <<EOF
Install OrbStack in one step, then this installer continues:

  ${ORBSTACK_DOWNLOAD}

  1. Download OrbStack
  2. Open the file and drag OrbStack to Applications
  3. Open OrbStack and finish its setup (it may ask for your Mac password)

EOF
  open "${ORBSTACK_DOWNLOAD}" 2>/dev/null || say "Open this page in your browser: ${ORBSTACK_DOWNLOAD}"
  wait_for_orbstack_app
}

# ---------------------------------------------------------------- Linux

linux_arch_ok() {
  case "$(uname -m)" in
    x86_64|amd64|aarch64|arm64) return 0 ;;
    *) return 1 ;;
  esac
}

# shellcheck disable=SC2016  # shown to the user literally
LINUX_DOCKER_COMMANDS='curl -fsSL https://get.docker.com | sudo sh
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER"'

# Docker Engine from Docker's own install script, with the user's consent.
install_linux_docker() {
  can_sudo || needs_user "Installing Docker needs administrator rights, and sudo can't ask for a password here.
Run these in your own terminal, then run the Stealthbox installer again:
  ${LINUX_DOCKER_COMMANDS}"
  say "Installing Docker Engine with Docker's install script (https://get.docker.com)..."
  curl -fsSL --retry 3 https://get.docker.com | as_root sh
  enable_linux_docker
}

# Start the daemon and let this user reach it. The docker group only applies
# after logging in again, so also grant this user the socket for this session.
enable_linux_docker() {
  if command -v systemctl >/dev/null 2>&1; then
    as_root systemctl enable --now docker
  elif command -v rc-update >/dev/null 2>&1; then
    as_root rc-update add docker && as_root service docker start
  else
    as_root service docker start
  fi
  user_name="$(id -un)"
  if [ "$user_name" != root ]; then
    as_root usermod -aG docker "$user_name" 2>/dev/null || as_root addgroup "$user_name" docker
    ensure_setfacl
    as_root setfacl -m "user:${user_name}:rw" /var/run/docker.sock
  fi
}

# setfacl grants this user the Docker socket now, without logging out and in;
# minimal images (Ubuntu cloud/server, Debian slim) don't ship it.
ensure_setfacl() {
  command -v setfacl >/dev/null 2>&1 && return 0
  if command -v apt-get >/dev/null 2>&1; then
    as_root apt-get install -y -qq acl >/dev/null
  elif command -v dnf >/dev/null 2>&1; then
    as_root dnf install -y -q acl
  elif command -v yum >/dev/null 2>&1; then
    as_root yum install -y -q acl
  elif command -v zypper >/dev/null 2>&1; then
    as_root zypper --non-interactive install acl
  elif command -v apk >/dev/null 2>&1; then
    as_root apk add -q acl
  fi
  command -v setfacl >/dev/null 2>&1 || needs_user "Docker is set up, but this session can't use it until you log out and back in
(the docker group applies at login). Do that, then run the Stealthbox installer again."
}

linux_docker_info() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 30 docker info 2>&1
  else
    docker info 2>&1
  fi
}

ensure_linux_docker() {
  linux_arch_ok || fail "Stealthbox on Linux supports amd64 (x86_64) and arm64 (aarch64). This machine is $(uname -m)."
  need bash "Stealthbox's commands are bash scripts. Install bash (for example 'sudo apk add bash' on Alpine), then run this again."
  if docker_is_podman; then
    fail "The 'docker' command here is Podman's Docker emulation (podman-docker), and Stealthbox needs Docker Engine.
Replace it with Docker Engine, then run this installer again:
  sudo dnf remove podman-docker      (or: sudo apt-get remove podman-docker)
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker \"\$USER\"   then log out and back in"
  fi
  if ! command -v docker >/dev/null 2>&1; then
    install_docker_consented || needs_docker "Stealthbox runs its browser in a container, and Docker Engine is not installed.
It would install Docker Engine with Docker's official script and let this user use it:
  ${LINUX_DOCKER_COMMANDS}"
    install_linux_docker
  fi
  info="$(linux_docker_info)" && status=0 || status=$?
  if [ "$status" -ne 0 ]; then
    case "$info" in
      *"permission denied"*|*"Cannot connect"*|*"Is the docker daemon running"*|*"No such file"*)
        install_docker_consented || needs_docker "Docker is installed but this user can't use it yet:
${info}
It would start Docker and add this user to the docker group (needs sudo)."
        can_sudo || needs_user "Docker needs to be started and this user added to the docker group, and sudo can't ask for a password here.
Run these in your own terminal, then run the Stealthbox installer again:
  sudo systemctl enable --now docker
  sudo usermod -aG docker \"\$USER\""
        enable_linux_docker
        info="$(linux_docker_info)" || needs_user "Docker is set up, but this session still can't reach it:
${info}
Log out and back in (so the docker group applies), then run the Stealthbox installer again."
        ;;
      *)
        if [ "$status" -eq 124 ]; then
          fail "'docker info' did not answer within 30 seconds. Restart Docker (sudo systemctl restart docker), then run this installer again."
        fi
        fail "'docker info' failed:
${info}
Fix Docker so 'docker info' works for this user, then run this installer again."
        ;;
    esac
  fi
  if ! docker compose version >/dev/null 2>&1; then
    fail "Docker is running, but the 'docker compose' plugin is missing.
Install it, then run this installer again:
  Debian/Ubuntu (Docker's packages):  sudo apt-get install docker-compose-plugin
  Debian/Ubuntu (distro packages):    sudo apt-get install docker-compose-v2
  Fedora/RHEL:                        sudo dnf install docker-compose-plugin
  Other:                              https://docs.docker.com/compose/install/linux/"
  fi
  say "Container engine: Docker Engine."
}

# ---------------------------------------------------------------- the app

# Run as ./install.sh from a clone: use that clone in place (for development).
checkout_dir() {
  case "$0" in
    *install.sh) ;;
    *) return 1 ;;
  esac
  [ -f "$0" ] || return 1
  dir="$(cd "$(dirname "$0")" && pwd)"
  [ -f "${dir}/stealthbox" ] && [ -f "${dir}/docker-compose.yml" ] || return 1
  say "$dir"
}

install_app() {
  found="$(checkout_dir || true)"
  if [ -n "$found" ]; then
    INSTALL_DIR="$found"
    say "Using the Stealthbox clone at ${INSTALL_DIR}"
    return 0
  fi
  mkdir -p "$STATE_DIR"
  rm -rf "${STATE_DIR}"/.download.*            # leftovers from an interrupted run
  # Stage inside STATE_DIR so the final swap is a same-filesystem rename.
  stage="$(mktemp -d "${STATE_DIR}/.download.XXXXXX")"
  say "Downloading Stealthbox..."
  curl -fsSL --retry 3 --connect-timeout 30 --max-time 600 -o "${stage}/app.tar.gz" "$TARBALL_URL"
  mkdir "${stage}/x"
  tar -xzf "${stage}/app.tar.gz" -C "${stage}/x"
  new=""
  for d in "${stage}"/x/*; do
    if [ -f "${d}/stealthbox" ] && [ -f "${d}/docker-compose.yml" ]; then
      new="$d"
    fi
  done
  if [ -z "$new" ]; then
    rm -rf "$stage"
    fail "The download from ${TARBALL_URL} doesn't look like Stealthbox."
  fi
  if [ -f "${INSTALL_DIR}/.env" ]; then
    cp -p "${INSTALL_DIR}/.env" "${new}/.env"
  fi
  if [ -e "$INSTALL_DIR" ]; then
    rm -rf "${STATE_DIR}/app.previous"
    mv "$INSTALL_DIR" "${STATE_DIR}/app.previous"
    mv "$new" "$INSTALL_DIR"
    say "Updated Stealthbox. The previous copy is in ${STATE_DIR}/app.previous"
  else
    mv "$new" "$INSTALL_DIR"
    say "Installed Stealthbox in ${INSTALL_DIR}"
  fi
  rm -rf "$stage"
  chmod +x "${INSTALL_DIR}/stealthbox" "${INSTALL_DIR}"/*.sh
}

add_path_line() {
  rc="$1"
  # shellcheck disable=SC2016  # the line is written literally into the rc file
  line='export PATH="$HOME/.local/bin:$PATH"'
  if [ -f "$rc" ] && grep -qxF "$line" "$rc"; then
    return 0
  fi
  printf '\n%s\n' "$line" >> "$rc"
}

link_cli() {
  target="${INSTALL_DIR}/stealthbox"
  if [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then
    ln -sf "$target" /usr/local/bin/stealthbox
    say "Installed the stealthbox command in /usr/local/bin."
    return 0
  fi
  mkdir -p "${HOME}/.local/bin"
  ln -sf "$target" "${HOME}/.local/bin/stealthbox"
  # Login and interactive shells for zsh and bash. Fish users add ~/.local/bin themselves.
  add_path_line "${HOME}/.profile"
  add_path_line "${HOME}/.zprofile"
  add_path_line "${HOME}/.zshrc"
  add_path_line "${HOME}/.bashrc"
  if [ -f "${HOME}/.bash_profile" ]; then
    add_path_line "${HOME}/.bash_profile"
  fi
  say "Installed the stealthbox command in ${HOME}/.local/bin (new terminals pick it up)."
}

main() {
  preflight
  case "$(uname -s)" in
    Darwin) ensure_mac_engine ;;
    Linux) ensure_linux_docker ;;
    *) fail "This installer is for macOS and Linux. On Windows, see https://github.com/jonclegg/stealthbox#quickstart" ;; # // pragma: allowlist secret
  esac
  install_app
  link_cli
  say "Starting Stealthbox..."
  "${INSTALL_DIR}/stealthbox" start "$@" </dev/null
}

main "$@"

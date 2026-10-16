#!/bin/bash
# Runs after the upstream /entrypoint.sh has started Xvfb (:99) and openbox.
set -euo pipefail

DATA_DIR=/tmp/cdp-server
export STEALTHBOX_VIEWER_TOKENS=/tmp/viewer-tokens
VIEWER_TOKENS=$STEALTHBOX_VIEWER_TOKENS
PROFILE_DIR=/profile/default
PROFILE_KEY="${STEALTHBOX_FINGERPRINT_SEED:-__default__}"

apply_container_timezone() {
  [ -n "${TZ:-}" ] || return 0
  zone="/usr/share/zoneinfo/${TZ}"
  if [ ! -e "$zone" ]; then
    echo "Timezone ${TZ} is not installed (${zone})." >&2
    exit 1
  fi
  ln -snf "$zone" /etc/localtime
  printf '%s\n' "$TZ" > /etc/timezone
}

apply_container_timezone

mkdir -p "$DATA_DIR" "$PROFILE_DIR"
rm -f "$PROFILE_DIR"/SingletonLock "$PROFILE_DIR"/SingletonSocket "$PROFILE_DIR"/SingletonCookie

# cdp-server deletes <data-dir>/<key> on shutdown but refuses to delete paths that
# resolve outside <data-dir>, so a symlink keeps the profile alive on the volume.
# The default identity's seed lives in that profile too (.cdp-server-seed), so the
# fingerprint survives restarts along with the cookies.
ln -sfn "$PROFILE_DIR" "$DATA_DIR/$PROFILE_KEY"

x11vnc -display :99 -forever -shared -nopw -localhost -rfbport 5900 -quiet -bg -o /tmp/x11vnc.log
websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/websockify.log 2>&1 &

# Session browsers (one per session, each on its own display, from a copy of the
# profile) do not outlive cdp-server: clear what a previous run left. Their live
# views go through one websockify that finds each display by the token file
# cdp-server writes for it.
rm -rf "$DATA_DIR"/s_* "$VIEWER_TOKENS"
mkdir -p "$VIEWER_TOKENS"
websockify --token-plugin TokenFile --token-source "$VIEWER_TOKENS" 6081 >/tmp/websockify-sessions.log 2>&1 &

# The chat API. It connects to the browser over loopback on first use, so it can
# start before Chrome is up. If it dies, it starts again: once it aborted in native
# code (heap corruption, cause not found), and the container stayed up with no API
# until someone restarted it. faulthandler prints every thread's stack when that happens.
run_api() {
  while true; do
    python -X faulthandler -u /opt/stealthbox/agent/server.py \
      || echo "stealthbox api exited with status $?; starting it again" >&2
    sleep 2
  done
}
run_api &

# --geoip sets the browser's timezone and locale from the egress IP (the proxy's,
# when one is set), so the clock always matches where the traffic comes from.
args=(--headless=false --geoip --data-dir="$DATA_DIR")
if [ -n "${STEALTHBOX_FINGERPRINT_SEED:-}" ]; then
  args+=(--fingerprint="$STEALTHBOX_FINGERPRINT_SEED")
fi
if [ "${STEALTHBOX_PERSONA:-windows}" = "macos" ]; then
  # Host fonts are mounted under /usr/local/share/fonts by docker-compose.mac.yml.
  # Generic families resolve to the Mac's choices (mac-fonts.conf); system-ui is
  # Chrome's UI font on Linux, which it takes from GTK.
  cp /opt/stealthbox/mac-fonts.conf /etc/fonts/local.conf
  mkdir -p /root/.config/gtk-3.0
  printf '[Settings]\ngtk-font-name=.SF NS 13\n' > /root/.config/gtk-3.0/settings.ini
  fc-cache -f >/dev/null
  args+=(--fingerprint-platform=macos --force-device-scale-factor=2)
fi
if [ -n "${STEALTHBOX_PROXY:-}" ]; then
  args+=(--proxy-server="$STEALTHBOX_PROXY")
fi

exec cdp-server "${args[@]}" "$@"

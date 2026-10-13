#!/bin/bash
# Runs after the upstream /entrypoint.sh has started Xvfb (:99) and openbox.
set -euo pipefail

DATA_DIR=/tmp/cdp-server
PROFILE_DIR=/profile/default
PROFILE_KEY="${HOMEBROWSER_FINGERPRINT_SEED:-__default__}"

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

# The chat API. It connects to the browser over loopback on first use, so it can
# start before Chrome is up. The health check covers it, so a crash shows.
python -u /opt/homebrowser/agent/server.py &

# --geoip sets the browser's timezone and locale from the egress IP (the proxy's,
# when one is set), so the clock always matches where the traffic comes from.
args=(--headless=false --geoip --data-dir="$DATA_DIR")
if [ -n "${HOMEBROWSER_FINGERPRINT_SEED:-}" ]; then
  args+=(--fingerprint="$HOMEBROWSER_FINGERPRINT_SEED")
fi
if [ "${HOMEBROWSER_PERSONA:-windows}" = "macos" ]; then
  # Host fonts are mounted under /usr/local/share/fonts by docker-compose.mac.yml.
  # Generic families resolve to the Mac's choices (mac-fonts.conf); system-ui is
  # Chrome's UI font on Linux, which it takes from GTK.
  cp /opt/homebrowser/mac-fonts.conf /etc/fonts/local.conf
  mkdir -p /root/.config/gtk-3.0
  printf '[Settings]\ngtk-font-name=.SF NS 13\n' > /root/.config/gtk-3.0/settings.ini
  fc-cache -f >/dev/null
  args+=(--fingerprint-platform=macos --force-device-scale-factor=2)
fi
if [ -n "${HOMEBROWSER_PROXY:-}" ]; then
  args+=(--proxy-server="$HOMEBROWSER_PROXY")
fi

exec cdp-server "${args[@]}" "$@"

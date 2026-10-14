#!/bin/bash
# Clean up any stale Xvfb lock left behind by a previous container instance.
# `/tmp` is not a tmpfs in this image, so on `docker restart` the previous
# container's `/tmp/.X99-lock` survives, and Xvfb refuses to start with an
# existing lock — leaving the container with no X server, every Chrome
# launch dying with "Missing X server or $DISPLAY", and `cdp-server`
# returning 502 forever.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# Start Xvfb for headed mode (challenge widgets), then run user command.
# STEALTHBOX_DISPLAY_SIZE sets the virtual screen; the macOS persona reports a
# Retina DPR of 2 only on a large display (e.g. 3840x2160).
Xvfb :99 -screen 0 "${STEALTHBOX_DISPLAY_SIZE:-1920x1080}x24" -nolisten tcp &

# Wait for the X server to actually accept connections before starting the WM.
# A blind `sleep 1` races under a CPU-starved start: openbox can come up before
# X is ready, fail to connect, and never retry — leaving --start-maximized a
# silent no-op for the container's whole life. Poll instead (xdotool is already
# installed and needs a live X server to answer). Bounded to ~10s.
for _ in $(seq 1 50); do
  DISPLAY=:99 xdotool getdisplaygeometry >/dev/null 2>&1 && break
  sleep 0.2
done

# Window manager so headed --start-maximized is honored (bare Xvfb has no WM;
# without one the flag is a silent no-op and the window stays un-maximized).
DISPLAY=:99 openbox &

exec "$@"

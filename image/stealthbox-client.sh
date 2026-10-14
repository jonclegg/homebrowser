#!/bin/sh
# The chat API client, run inside the container: docker exec stealthbox stealthbox <command>
export STEALTHBOX_DATA_DIR=/data
export STEALTHBOX_API_URL="http://127.0.0.1:${STEALTHBOX_API_LISTEN_PORT:-8423}"
exec python /opt/stealthbox/agent/client.py "$@"

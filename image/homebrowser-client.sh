#!/bin/sh
# The chat API client, run inside the container: docker exec homebrowser homebrowser <command>
export HOMEBROWSER_DATA_DIR=/data
export HOMEBROWSER_API_URL="http://127.0.0.1:${HOMEBROWSER_API_LISTEN_PORT:-8423}"
exec python /opt/homebrowser/agent/client.py "$@"

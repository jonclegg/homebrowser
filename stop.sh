#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

source ./lib.sh

if ! docker info >/dev/null 2>&1; then
  echo "The container engine is not running, so Homebrowser is already stopped."
  exit 0
fi

docker compose --profile examples --profile share down
echo
echo "Homebrowser stopped. Your browser profile is saved; ./homebrowser start brings it back."

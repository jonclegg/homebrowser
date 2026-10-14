#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

source ./lib.sh

if ! docker info >/dev/null 2>&1; then
  echo "The container engine is not running, so Stealthbox is already stopped."
  exit 0
fi

docker compose --profile examples --profile share down
echo
echo "Stealthbox stopped. Your browser profile is saved; ./stealthbox start brings it back."

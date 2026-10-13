#!/usr/bin/env bash
# Runs on the build instance (build-release.sh copies it to /work): install the
# build dependencies, then build and pack both Linux cores. arm64 is cross-compiled
# on the x64 instance. The log is /work/build-all.log; its last line says how it ended.
cd /work/fury/core
run() {
  echo "==== build deps $(date -u)"
  sudo src/build/install-build-deps.sh --no-prompt --no-chromeos-fonts > /work/deps.log 2>&1 || { echo "DEPS-FAILED"; return 1; }
  for target in linux-x64 linux-arm64; do
    echo "==== $target $(date -u)"
    build/build.sh "$target" || { echo "BUILD-FAILED $target"; return 1; }
    release/pack-core-linux.sh "$target" || { echo "PACK-FAILED $target"; return 1; }
  done
  echo "ALL-DONE $(date -u)"
}
run > /work/build-all.log 2>&1
echo "EXIT=$?" >> /work/build-all.log

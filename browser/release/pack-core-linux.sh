#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 Bogdan Shapovalov and the Fury authors
#
# Pack a Linux core into a tarball.
#
#     browser/release/pack-core-linux.sh linux-x64
#     browser/release/pack-core-linux.sh linux-arm64
#
# The Linux half of pack-core-windows.sh, and an explicit list for the same
# reason: the output directory is build tree, and what the browser needs to run
# is a small part of it.
#
# No chrome-sandbox. The setuid helper only does anything once it is root-owned
# and setuid 4755, which a tarball cannot carry, and build.sh builds `chrome`,
# not the separate chrome_sandbox target. Without it the browser uses the
# user-namespace sandbox, which is what a container normally offers anyway.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${1:?usage: pack-core-linux.sh <linux-x64|linux-arm64>}"
out="${OUT_DIR:-$here/src/out/$target.noindex}"
dist="${DIST:-$here/dist}"

cd "$out"

# The same guard as Windows: an instrumented build is a different browser.
if grep -a -q '__llvm_profile' chrome 2>/dev/null; then
  echo "!! chrome carries the PGO instrumentation runtime (__llvm_profile)." >&2
  echo "!! Set chrome_pgo_phase = 2 in the args and rebuild." >&2
  exit 1
fi

files=(
  chrome chrome_crashpad_handler
  libEGL.so libGLESv2.so libvk_swiftshader.so libvulkan.so.1 vk_swiftshader_icd.json
  chrome_100_percent.pak chrome_200_percent.pak resources.pak
  icudtl.dat v8_context_snapshot.bin
)
# Present or not depending on the args (use_qt, the snapshot kind); taken when
# they are there.
optional=(libqt5_shim.so libqt6_shim.so snapshot_blob.bin xdg-mime xdg-settings)
dirs=(locales MEIPreload PrivacySandboxAttestationsPreloaded)

missing=0
for f in "${files[@]}"; do
  [ -f "$f" ] || { echo "!! missing: $f" >&2; missing=1; }
done
[ "$missing" = 0 ] || exit 1

stage="$here/dist-core/$target/Fury"
rm -rf "$stage" && mkdir -p "$stage"
cp "${files[@]}" "$stage/"
for f in "${optional[@]}"; do [ -f "$f" ] && cp "$f" "$stage/"; done
for d in "${dirs[@]}"; do [ -d "$d" ] && cp -r "$d" "$stage/"; done
cp "$here/CHROMIUM_VERSION" "$stage/"

echo "== staged"
du -sh "$stage" | cut -f1
echo "== packing"
mkdir -p "$dist"
tar -cJf "$dist/fury-core-$target.tar.xz" -C "$here/dist-core/$target" Fury
ls -la "$dist/fury-core-$target.tar.xz" | awk '{printf "   %.0f MB\n", $5/1000000}'

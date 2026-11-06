#!/usr/bin/env bash
# Rebuilds the vendored mapd binary from upstream source plus our parallel-download patch.
#
# The vendored binary is NOT the upstream release artifact: it carries
# mapd_parallel.patch (concurrent tile downloads, see README.md). Re-run this whenever
# bumping the mapd version in sunnypilot/mapd/mapd_installer.py, then update
# MAPD_SHA256 in the same file with the new hash and copy the new patch alongside.
set -euo pipefail

MAPD_VERSION="${MAPD_VERSION:-v1.12.0}"
WORKDIR="$(mktemp -d)/mapd"
OUT="${1:-$(pwd)/mapd}"

trap 'rm -rf "$(dirname "$WORKDIR")"' EXIT

git clone --quiet --depth 1 --branch "$MAPD_VERSION" https://github.com/pfeiferj/mapd.git "$WORKDIR"
git -C "$WORKDIR" apply "$(dirname "$0")/mapd_parallel.patch"

# Match upstream's Earthfile build flags; CGO_ENABLED=0 keeps it fully static and
# avoids the czlib/pkg-config cgo path (paulmach/osm falls back to compress/zlib).
( cd "$WORKDIR" && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 \
  go build -trimpath -ldflags="-s -w" -o "$OUT" . )

echo "built $OUT ($(du -h "$OUT" | cut -f1))"
echo "sha256: $(shasum -a 256 "$OUT" | cut -d' ' -f1)"
echo "then update MAPD_SHA256 in sunnypilot/mapd/mapd_installer.py and sunnypilot/mapd/tests/mapd_hash"

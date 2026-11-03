#!/usr/bin/env bash
# Vendor the comma-deps-* wheels (aarch64) into the stripped tree.
#
# Why: the on-device scons build (SConstruct) and runtime code import these
# packages, but older device venvs (e.g. the C3L clone's AGNOS 12.4) don't
# ship them. Unpacking them into third_party/pypi puts them on PYTHONPATH
# (see launch_chffrplus.sh), so installs are fully self-contained.
set -ex

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null && pwd)/../.."
TARGET_DIR="${1:?usage: vendor_device_deps.sh TARGET_DIR}"
VENDOR_DIR="$TARGET_DIR/third_party/pypi"

# the packages SConstruct imports by name; distribution names are comma-deps-<pkg>
PKG_NAMES=(acados capnproto eigen ffmpeg json11 ncurses zeromq zstd)
PKG_PATTERN="$(IFS='|'; echo "${PKG_NAMES[*]}")"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# resolve the exact pinned versions from uv.lock, then fetch aarch64 wheels
# (comma-deps-ncurses only lives in the "tools" extra, so export everything)
cd "$SOURCE_DIR"
uv export --frozen --all-extras --all-groups --no-hashes --no-annotate --no-emit-project \
  --format requirements-txt -o "$TMP/all.txt"
grep -E "^comma-deps-(${PKG_PATTERN})==" "$TMP/all.txt" > "$TMP/device.txt"
echo "[-] vendoring device deps:"; cat "$TMP/device.txt"

uv pip install --no-deps --target "$TMP/sp" \
  --python-platform aarch64-unknown-linux-gnu --python-version 3.12 \
  -r "$TMP/device.txt"

mkdir -p "$VENDOR_DIR"
find "$TMP/sp" -mindepth 1 -maxdepth 1 \
  ! -name '*.dist-info' ! -name '__pycache__' ! -name 'bin' \
  -exec cp -pR {} "$VENDOR_DIR/" \;

# wheel entry-point scripts (e.g. capnpc) get a shebang for the resolving
# interpreter; the device has these tools system-wide, so drop them
rm -rf "$TMP/sp/bin"

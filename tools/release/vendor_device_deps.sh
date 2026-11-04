#!/usr/bin/env bash
# Vendor the comma-deps-* wheels (aarch64) and the PyPI packages the manager/UI
# import chain needs into the stripped tree.
#
# Why: the on-device scons build (SConstruct) and runtime code import these
# packages, but older device venvs (e.g. the C3L clone's AGNOS 12.4) don't
# ship them. Unpacking them into third_party/pypi puts them on PYTHONPATH
# (see launch_chffrplus.sh), so installs are fully self-contained.
set -ex

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null && pwd)/../.."
TARGET_DIR="${1:?usage: vendor_device_deps.sh TARGET_DIR}"
VENDOR_DIR="$TARGET_DIR/third_party/pypi"

# the packages SConstruct imports by name (plus libusb for the pandad USB
# transport); distribution names are comma-deps-<pkg>
# NOTE: raylib/pyray are deliberately NOT vendored — the comma-deps-raylib
# build drives DRM directly ("magic" backend) while AGNOS rootfs images run
# weston; the rootfs venv's raylib (wayland backend) is the working one.
PKG_NAMES=(acados capnproto eigen ffmpeg json11 libusb ncurses zeromq zstd)
# plain-PyPI packages the boot path needs:
#   zstandard — top-level import in common/utils.py, so manager.py dies at
#               import (silent boot-logo hang) without it
#   jeepney   — UI settings wifi manager import chain
#   numpy/pycapnp/pyzmq/pillow — pin the uv.lock versions over whatever venv the
#               rootfs ships; venv vintage varies (e.g. the C3L clone's carrot
#               AGNOS 12.4 rootfs has numpy 2.1.3 / pycapnp 2.0.0, and SConstruct
#               imports numpy at build time, cereal binds pycapnp at runtime)
EXTRA_PKGS=(zstandard jeepney numpy pycapnp pyzmq pillow)
PKG_PATTERN="$(IFS='|'; echo "${PKG_NAMES[*]}")"
EXTRA_PATTERN="$(IFS='|'; echo "${EXTRA_PKGS[*]}")"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# resolve the exact pinned versions from uv.lock, then fetch aarch64 wheels
# (comma-deps-ncurses only lives in the "tools" extra, so export everything)
cd "$SOURCE_DIR"
uv export --frozen --all-extras --all-groups --no-hashes --no-annotate --no-emit-project \
  --format requirements-txt -o "$TMP/all.txt"
grep -E "^(comma-deps-(${PKG_PATTERN})|(${EXTRA_PATTERN}))==" "$TMP/all.txt" > "$TMP/device.txt"
echo "[-] vendoring device deps:"; cat "$TMP/device.txt"

uv pip install --no-deps --target "$TMP/sp" \
  --python-platform aarch64-unknown-linux-gnu --python-version 3.12 \
  -r "$TMP/device.txt"

mkdir -p "$VENDOR_DIR"
find "$TMP/sp" -mindepth 1 -maxdepth 1 \
  ! -name '*.dist-info' ! -name '__pycache__' ! -name 'bin' \
  -exec cp -pR {} "$VENDOR_DIR/" \;

# pip's top-level bin/ holds entry-point shims whose shebang points at the
# resolving interpreter (absent on device) — drop them. The real capnp
# codegen toolchain ships inside capnproto/install/bin; launch_chffrplus.sh
# puts that dir on PATH so codegen matches the vendored capnp headers/libs.
rm -rf "$TMP/sp/bin"

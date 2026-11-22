#!/usr/bin/env bash

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1

# models get lower priority than ui
# - ui is ~5ms
# - modeld is 20ms
# - DM is 10ms
# in order to run ui at 60fps (16.67ms), we need to allow
# it to preempt the model workloads. we have enough
# headroom for this until ui is moved to the CPU.
export QCOM_PRIORITY=12

if [ -z "$AGNOS_VERSION" ]; then
  # upgrade target: AGNOS 20.6.100, the custom tici build from agnos-builder
  # that is currently flashed on the device. /VERSION on device reports
  # exactly "20.6.100", and agnos.json ships the matching 20.6 partition
  # hashes (boot/system from the official commadist CDN), so the version
  # check passes and the updater never triggers.
  export AGNOS_VERSION="20.6.102"
fi

# AGNOS rootfs images run weston as the wayland compositor; pyray (spinner, ui)
# connects as a wayland client and needs these set before any window opens
if [ -d /var/tmp/weston ]; then
  export XDG_RUNTIME_DIR="/var/tmp/weston"
  export WAYLAND_DISPLAY="wayland-0"
fi

# C3L (comma 3X lite clone, reports as tici) has no cabin camera:
# camerad skips it entirely, and process_config.py gates the DM processes on this
export DISABLE_DRIVER=1

# BYD Song Plus DM-i: the DiPilot camera (MPC) faults with 'check multifunction
# video controller' when it sees the UDS/isotp firmware-query frames that card
# broadcasts on the powertrain bus during startup (relayed to bus 2 by the
# panda while still in elm327 mode). The platform is fixed via CAN auto-match,
# so the query is unnecessary - skip it entirely. card.py also sets this as a
# default (the env route alone proved unreliable on carrot's device).
export SKIP_FW_QUERY=1

export STAGING_ROOT="/data/safe_staging"

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
  # pin to the C3L clone's AGNOS so agnos_init never re-flashes official AGNOS onto it
  export AGNOS_VERSION="12.4"
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

export STAGING_ROOT="/data/safe_staging"

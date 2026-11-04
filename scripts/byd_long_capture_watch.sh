#!/bin/sh
# Watchdog for byd_long_capture.py (see that script's header for purpose).
# Device install: copy to /data/carrot/tools/, chmod +x, and add a
#   nohup /data/carrot/tools/byd_long_capture_watch.sh >/dev/null 2>&1 &
# line near the top of /data/continue.sh (outside git - survives repo updates).
# flock keeps it single-instance even if boot hook and a manual launch overlap.
exec 9>/tmp/byd_capture.lock
flock -n 9 || exit 0
cd /data/openpilot
while :; do
  pgrep -f "python3 /data/carrot/tools/byd_long_capture.py" >/dev/null || \
    PYTHONPATH=/data/openpilot nohup /usr/local/venv/bin/python3 /data/carrot/tools/byd_long_capture.py >>/data/carrot/capture/capture.log 2>&1 &
  sleep 20
done

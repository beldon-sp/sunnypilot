#!/usr/bin/env python3
"""BYD 纵向路测取证采集器(carrot 设备端)。

背景:设备 DongleId=UnregisteredDevice,loggerd 从不落 rlog(2026-10-05 实测
realdata 只有 boot 视频),纵向"设置不应控制/一直加油刹车"的定量分析没有数据源。
本脚本直接从 msgq 拉关心的 topic,写紧凑 JSONL 到 /data/carrot/capture/,拉回
本机用 python 分析即可。

设备端启动(ssh comma@<ip>):
  cd /data/openpilot && PYTHONPATH=/data/openpilot nohup \
    /usr/local/venv/bin/python3 /data/carrot/tools/byd_long_capture.py \
    >/data/carrot/capture/capture.log 2>&1 &
拉回:scp comma@<ip>:/data/carrot/capture/byd_long_*.jsonl /tmp/byd_roadtest/

记录内容:
- can: bus0/bus2 关键帧(0x316/0x318/0x32D/0x32E/0x32F/0x3B0/0x1F0/0x11F/0x242),
  地址+数据 hex+总线,全频
- carState/carControl/carOutput/selfdriveState:20 Hz 抽样 + enabled/state 变化沿全记
- longitudinalPlan:全频(20 Hz 原生),含 vCruise/aTarget/events
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime

from openpilot.cereal import messaging

# BYD CAN ids we care about (bus0 = chassis/control, bus2 = ACC domain).
# 0x351/2/3 + 0x374 are the vendor radar-track frames (Tang: 849-851 on bus1,
# Han: 884 on bus2) - recording them settles whether the Song Plus even has a
# usable lead-track message for a future radar interface.
CAN_WANT = {0x316, 0x318, 0x32D, 0x32E, 0x32F, 0x3B0, 0x1F0, 0x11F, 0x242, 0x133,
            0x351, 0x352, 0x353, 0x374}
SAMPLE_EVERY = 5   # 100 Hz topics -> 20 Hz

def carstate_row(cs):
  cr = cs.cruiseState
  return {"v": round(cs.vEgo, 3), "vc": round(cs.vEgoCluster, 3),
          "avail": cr.available, "en": cr.enabled, "ss": round(cr.speed, 3),
          "std": cr.standstill, "gas": cs.gasPressed, "brk": cs.brakePressed,
          "drv": cs.steeringTorque, "press": cs.steeringPressed,
          "gear": str(cs.gearShifter), "door": cs.doorOpen}

def carcontrol_row(cc):
  a = cc.actuators
  return {"en": cc.enabled, "lat": cc.latActive, "lon": cc.longActive,
          "accel": round(a.accel, 4), "jk": round(a.jerk, 4),
          "atgt": round(a.aTarget, 4), "vtgt": round(a.vTarget, 4) if hasattr(a, "vTarget") else None,
          "lcs": str(a.longControlState),
          "resume": cc.cruiseControl.resume, "cancel": cc.cruiseControl.cancel}

def caroutput_row(co):
  # carrot's CarOutput carries ONLY actuatorsOutput (no up/ui/ufAccelCmd)
  a = co.actuatorsOutput
  return {"accel": round(a.accel, 4),
          "torque": round(a.torque, 4), "torqueCan": round(a.torqueOutputCan, 1),
          "lcs": str(a.longControlState), "jerk": round(a.jerk, 4),
          "aTarget": round(a.aTarget, 4)}

def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--outdir", default="/data/carrot/capture")
  ap.add_argument("--seconds", type=float, default=0.0, help="0 = run until killed")
  args = ap.parse_args()

  os.makedirs(args.outdir, exist_ok=True)
  path = os.path.join(args.outdir, f"byd_long_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
  print(f"BYD long capture -> {path}", flush=True)

  sm = messaging.SubMaster(["can", "carState", "carControl", "carOutput",
                            "selfdriveState", "longitudinalPlan", "onroadEvents"])
  prev_enabled = None
  prev_events = ()
  seen_errs = set()  # one logged traceback per failing accessor, then silence
  t0 = time.monotonic()

  def build_rows():
    nonlocal prev_enabled, prev_events
    rows = []
    if sm.updated["can"]:
      for ce in sm["can"]:
        if ce.address in CAN_WANT:
          rows.append({"t": t, "k": "can", "a": hex(ce.address), "b": ce.src,
                       "d": bytes(ce.dat).hex()})
    if sm.updated["carState"]:
      rows.append({"t": t, "k": "cs", "r": carstate_row(sm["carState"])})
    if sm.updated["carControl"] and sm.frame % SAMPLE_EVERY == 0:
      rows.append({"t": t, "k": "cc", "r": carcontrol_row(sm["carControl"])})
    if sm.updated["carOutput"] and sm.frame % SAMPLE_EVERY == 0:
      rows.append({"t": t, "k": "co", "r": caroutput_row(sm["carOutput"])})
    if sm.updated["selfdriveState"]:
      sd = sm["selfdriveState"]
      if sd.enabled != prev_enabled:
        prev_enabled = sd.enabled
        rows.append({"t": t, "k": "sd_edge", "en": sd.enabled, "st": str(sd.state)})
      if sm.frame % SAMPLE_EVERY == 0:
        rows.append({"t": t, "k": "sd", "en": sd.enabled, "st": str(sd.state)})
    if sm.updated["longitudinalPlan"]:
      # carrot schema: no vCruiseCluster/events-OnroadEvent-as-text - use the
      # fork's own fields (cruiseTarget/aTarget/shouldStop/xState/trafficState)
      lp = sm["longitudinalPlan"]
      rows.append({"t": t, "k": "lp", "vc": round(lp.cruiseTarget, 3),
                   "at": round(lp.aTarget, 4), "stop": lp.shouldStop,
                   "th": lp.allowThrottle, "br": lp.allowBrake,
                   "x": lp.xState, "tr": lp.trafficState, "lead": lp.hasLead})
    if sm.updated["onroadEvents"]:
      # 100 Hz publisher - log edges only (this is what answers "why did OP
      # softDisable at the 13:45 CANCEL fragment")
      names = tuple(sorted(str(e.name) for e in sm["onroadEvents"]))
      nonlocal prev_events
      if names != prev_events:
        prev_events = names
        rows.append({"t": t, "k": "ev", "l": list(names)})
    return rows

  with open(path, "a") as f:
    f.write(json.dumps({"t": 0.0, "k": "start", "wall": datetime.now().isoformat(),
                        "topics": sm.services}) + "\n")
    f.flush()
    while True:
      sm.update(100)
      t = round(time.monotonic() - t0, 3)
      try:
        rows = build_rows()
      except Exception:
        # one accessor blowing up must never end the capture: log the shape
        # once, keep going (the 2026-10-05 vCruiseCluster crash-loop incident)
        key = traceback.format_exc(limit=1).strip().splitlines()[-1]
        if key not in seen_errs:
          seen_errs.add(key)
          rows = [{"t": t, "k": "err", "m": key}]
        else:
          rows = []
      for r in rows:
        f.write(json.dumps(r, separators=(",", ":")) + "\n")
      if rows:
        f.flush()
      if args.seconds and time.monotonic() - t0 > args.seconds:
        break
  print("done", flush=True)

if __name__ == "__main__":
  sys.exit(main())

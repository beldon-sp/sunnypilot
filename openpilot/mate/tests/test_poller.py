import json
import time

from openpilot.cereal import car, custom, log
from openpilot.mate.poller import (
  EVENT_FLAGS,
  FRAME_PERIOD_S,
  FrameTicker,
  SoundTracker,
  backup_snapshot,
  button_payloads,
  calib_snapshot,
  carrot_snapshot,
  car_params_snapshot,
  car_snapshot,
  car_sp_snapshot,
  control_snapshot,
  controls_snapshot,
  device_snapshot,
  diff_events,
  dmodel_snapshot,
  enum_name,
  event_to_dict,
  frame_payload,
  gps_snapshot,
  loc_snapshot,
  manager_snapshot,
  map_snapshot,
  model_snapshot,
  odom_snapshot,
  opstate_payload,
  panda_payload,
  params_snapshot,
  peripheral_snapshot,
  plan_snapshot,
  proc_snapshot,
  radar_snapshot,
  services_status,
  sound_payload,
  temp_snapshot,
)
from openpilot.selfdrive.ui.soundd import AudibleAlert


class FakeSM:
  """最小 SubMaster 替身：updated / 下标 / recv_time / 健康度属性。"""

  def __init__(self, **messages):
    self._messages = dict(messages)
    self.services = list(self._messages)
    self.updated = dict.fromkeys(self._messages, True)
    now = time.monotonic()
    self.recv_time = dict.fromkeys(self._messages, now)
    self.frame = 1
    self.seen = dict.fromkeys(self._messages, True)
    self.alive = dict.fromkeys(self._messages, True)
    self.valid = dict.fromkeys(self._messages, True)
    self.logMonoTime = dict.fromkeys(self._messages, int(now * 1e9))

  def __getitem__(self, key):
    return self._messages[key]


def selfdrive_state(**kwargs):
  defaults = {"state": "disabled", "enabled": False, "active": False, "engageable": False,
              "alertSound": "none", "alertText1": "", "alertText2": ""}
  return log.SelfdriveState.new_message(**{**defaults, **kwargs})


def make_sm(ss=None, ss_sp=None, carrot=None, cs=None, events=None):
  sm = FakeSM(
    selfdriveState=ss or selfdrive_state(),
    selfdriveStateSP=ss_sp or custom.SelfdriveStateSP.new_message(),
    carrotMan=carrot or custom.CarrotMan.new_message(leftSec=100),
    carState=cs or car.CarState.new_message(),
    onroadEvents=events or log.OnroadEvent.new_message(),
  )
  return sm


class TestEvents:
  def test_event_to_dict_all_flags(self):
    e = log.OnroadEvent.new_message(name="doorOpen", warning=True, noEntry=True)
    d = event_to_dict(e)
    assert d["name"] == "doorOpen"
    assert d["warning"] and d["noEntry"]
    assert not d["softDisable"]
    assert set(d) == {"name", *EVENT_FLAGS}

  def test_diff_added_removed(self):
    prev: dict[str, dict] = {}
    e1 = event_to_dict(log.OnroadEvent.new_message(name="doorOpen", warning=True))
    prev, payload = diff_events(prev, [e1])
    assert payload is not None
    assert payload["added"] == ["doorOpen"] and payload["removed"] == []
    assert payload["active"] == [e1]

    e2 = event_to_dict(log.OnroadEvent.new_message(name="seatbeltNotLatched", warning=True))
    prev, payload = diff_events(prev, [e1, e2])
    assert payload["added"] == ["seatbeltNotLatched"] and payload["removed"] == []

    prev, payload = diff_events(prev, [e2])
    assert payload["added"] == [] and payload["removed"] == ["doorOpen"]

    prev, payload = diff_events(prev, [e2])
    assert payload is None  # 无变化不推

  def test_diff_replaces_state_on_noop(self):
    # diff_events 即使不产生 payload 也要返回可继续累积的 map（同一对象原样返回）
    e = event_to_dict(log.OnroadEvent.new_message(name="canError", permanent=True))
    prev, payload = diff_events({}, [e])
    prev2, payload2 = diff_events(prev, [e])
    assert payload2 is None
    assert prev2 is prev


class TestOpstateButton:
  def test_opstate_payload(self):
    ss = selfdrive_state(state="enabled", enabled=True, active=True, engageable=True)
    p = opstate_payload(ss)
    assert p == {"state": "enabled", "enabled": True, "active": True, "engageable": True}

  def test_button_payloads(self):
    cs = car.CarState.new_message(buttonEvents=[
      {"type": "setCruise", "pressed": True},
      {"type": "setCruise", "pressed": False},
    ])
    assert button_payloads(cs) == [
      {"button": "setCruise", "pressed": True},
      {"button": "setCruise", "pressed": False},
    ]

  def test_enum_name_unknown(self):
    assert enum_name(AudibleAlert, 99999) == "unknown"


class TestCarSnapshot:
  def test_car_snapshot_fields(self):
    cs = car.CarState.new_message(
      vEgo=10.0, aEgo=0.5, standstill=False, gearShifter="drive",
      cruiseState={"enabled": True, "available": True, "speed": 12.5, "speedCluster": 12.3,
                   "standstill": False, "nonAdaptive": False},
      vCruise=12.5, doorOpen=True,
      leftBlindspot=True, rightBlindspot=False,
      fuelGauge=0.42, charging=True,
    )
    # 本仓无 CarState.latEnabled/carrotCruise：latEnabled 由 MADS 推导
    mads = custom.ModularAssistiveDrivingSystem.new_message(
      enabled=True, available=True, state="enabled")
    ss_sp = custom.SelfdriveStateSP.new_message(mads=mads)
    sm = make_sm(ss_sp=ss_sp, cs=cs)
    snap = car_snapshot(cs, sm)
    assert snap["vEgo"] == 10.0 and snap["vEgoKph"] == 36.0
    assert snap["gearShifter"] == "drive"
    assert snap["cruiseState"]["speedKph"] == 45.0
    assert snap["cruiseState"]["speedCluster"] == 12.3  # 原样 m/s 直传，App 侧负责换算
    # vCruise 本身即 kph（card.py 取自 VCruiseHelper）：vCruiseKph 必须原样直传，勿乘 3.6
    assert snap["vCruise"] == 12.5 and snap["vCruiseKph"] == 12.5
    assert snap["doorOpen"] is True
    assert snap["latEnabled"] is True  # MADS enabled ∧ 非 paused
    assert snap["carrotCruise"] is False  # 本仓无此字段，恒 False 稳定帧结构
    assert snap["speedLimit"] == 0.0 and snap["engineRpm"] == 0.0  # 本仓无此字段，恒 0
    assert snap["leftBlindspot"] is True and snap["rightBlindspot"] is False  # 盲区直通
    assert snap["fuelGauge"] == 0.42 and snap["charging"] is True  # 车辆健康（v3 新增）
    assert json.dumps(snap)  # 必须始终可序列化

  def test_car_snapshot_defaults_readable(self):
    # 从未 update 过的 CarState（offroad 首帧）也要能出快照，不能抛
    sm = make_sm()
    snap = car_snapshot(car.CarState.new_message(), sm)
    assert snap["standstill"] is False and snap["gearShifter"] == "unknown"
    assert snap["leftBlindspot"] is False and snap["rightBlindspot"] is False

  def test_lat_enabled_falls_back_to_selfdrive_state(self):
    cs = car.CarState.new_message()
    sm = make_sm(ss=selfdrive_state(active=True), cs=cs)  # MADS unavailable
    assert car_snapshot(cs, sm)["latEnabled"] is True
    sm = make_sm(ss=selfdrive_state(active=False), cs=cs)
    assert car_snapshot(cs, sm)["latEnabled"] is False


class TestServiceStatus:
  def test_shape_and_age(self):
    sm = make_sm()
    status = services_status(sm)
    assert set(status) == set(sm.services)
    for entry in status.values():
      assert entry["seen"] and entry["alive"] and entry["valid"]
      assert 0 <= entry["ageS"] < 1.0
    json.dumps(status)  # 必须始终可序列化

  def test_unseen_topic(self):
    # offroad 场景：carState 一帧没收过；alive 对 on-demand topic 会恒真，
    # 全靠 seen 字段区分「数据源没跑」
    sm = make_sm()
    sm.seen["carState"] = False
    sm.alive["carState"] = False
    entry = services_status(sm)["carState"]
    assert entry["seen"] is False and entry["alive"] is False and entry["ageS"] is None


class TestPandaPayload:
  def test_full_payload(self):
    ps = [log.PandaState.new_message(
      heartbeatLost=True, controlsAllowed=False, safetyModel="byd", safetyParam=1,
      harnessStatus="notConnected", uptime=42, faults=["relayMalfunction"],
      canState0={"busOff": True, "busOffCnt": 3, "canSpeed": 500, "totalRxCnt": 10},
    )]
    p = panda_payload(ps)
    assert p["count"] == 1
    d = p["pandas"][0]
    assert d["heartbeatLost"] is True and d["safetyModel"] == "byd" and d["safetyParam"] == 1
    assert d["faultStatus"] == "none" and d["faults"] == ["relayMalfunction"]
    assert d["harnessStatus"] == "notConnected" and d["uptime"] == 42
    assert len(d["canStates"]) == 3
    bus0 = d["canStates"][0]
    assert bus0["bus"] == 0 and bus0["busOff"] is True and bus0["canSpeed"] == 500
    json.dumps(p)  # 必须始终可序列化

  def test_defaults_readable(self):
    # 全默认字段（未 set）的 PandaState 也不能抛
    p = panda_payload([log.PandaState.new_message()])
    assert p["pandas"][0]["heartbeatLost"] is False and p["pandas"][0]["faults"] == []


class TestSoundTracker:
  def test_alert_passthrough_and_dedupe(self):
    t = SoundTracker()
    sm = make_sm(ss=selfdrive_state(alertSound="warningImmediate", alertText1="BRAKE!", alertText2="Risk"))
    p = t.update(sm, main_toggle=False)
    assert p == {"sound": "warningImmediate", "file": "dm_critical.wav", "loop": True,
                 "alertText1": "BRAKE!", "alertText2": "Risk"}
    assert t.update(sm, main_toggle=False) is None  # 同一 alert 不重复推
    sm = make_sm(ss=selfdrive_state(alertSound="none"))
    p = t.update(sm, main_toggle=False)
    assert p["sound"] == "none" and p["file"] is None and p["loop"] is False

  def test_carrot_countdown_synthesis(self):
    t = SoundTracker()
    # 初值 0、leftSec 100：soundd 语义下 100 不合成任何音效（非 0..11），且会记录 count_down
    sm = make_sm(carrot=custom.CarrotMan.new_message(leftSec=100))
    assert t.update(sm, main_toggle=False) is None
    sm = make_sm(carrot=custom.CarrotMan.new_message(leftSec=3))
    p = t.update(sm, main_toggle=False)
    # 本仓 AudibleAlert 无 audioN 成员：倒计时直接按文件名下发，loop=False 单次播放
    assert p["sound"] == "audio3" and p["file"] == "audio_3.wav" and p["loop"] is False
    sm = make_sm(carrot=custom.CarrotMan.new_message(leftSec=11))
    assert t.update(sm, main_toggle=False)["sound"] == "promptDistracted"
    sm = make_sm(carrot=custom.CarrotMan.new_message(leftSec=1))
    assert t.update(sm, main_toggle=False)["sound"] == "audio1"
    sm = make_sm(carrot=custom.CarrotMan.new_message(leftSec=0))
    p = t.update(sm, main_toggle=False)
    assert p["sound"] == "longDisengaged" and p["file"] == "tici_disengaged.wav" and p["loop"] is True
    sm = make_sm(carrot=custom.CarrotMan.new_message(leftSec=100))
    assert t.update(sm, main_toggle=False)["sound"] == "none"

  def test_alert_sound_wins_over_countdown(self):
    t = SoundTracker()
    sm = make_sm(ss=selfdrive_state(alertSound="prompt"), carrot=custom.CarrotMan.new_message(leftSec=5))
    p = t.update(sm, main_toggle=False)
    assert p["sound"] == "prompt"

  def test_main_toggle_short_circuits_to_prompt(self):
    t = SoundTracker()
    sm = make_sm(ss=selfdrive_state(alertSound="warningImmediate"))
    p = t.update(sm, main_toggle=True)
    assert p["sound"] == "prompt"

  def test_sound_payload_unknown_alert(self):
    # 未收录进 sound_list 的值：file/loop 兜底，与 soundd 的 loaded_sounds 检查对应
    p = sound_payload(99999, selfdrive_state())
    assert p["sound"] == "unknown" and p["file"] is None and p["loop"] is False


class TestRadarSnapshot:
  def test_lead_field_mapping(self):
    # 字段名与 carrotmate Lead 模型一致：trackId ← cereal radarTrackId
    # 本仓 radarState 无 leadsCenter/leadLeft/leadRight、LeadData 无 aRel/fcw/dPath：
    # 侧向键恒空 lead，aRel/fcw/dPath 恒 0/False，键位保留稳定客户端帧结构
    rs = log.RadarState.new_message(
      radarErrors={"canError": True, "radarFault": True},
      leadOne={"present": True, "dRel": 30.567, "yRel": -1.2, "vRel": -5.0,
               "vLead": 25.0, "radarTrackId": 42},
    )
    snap = radar_snapshot(rs)
    assert snap["leadOne"] == {
      "status": True, "dRel": 30.57, "yRel": -1.2, "vRel": -5.0, "aRel": 0.0,
      "vLead": 25.0, "dPath": 0.0, "fcw": False, "trackId": 42,
    }
    assert snap["leadsCenterCount"] == 0
    assert snap["leadLeft"]["status"] is False and snap["leadRight"]["status"] is False
    assert snap["radarErrors"] == ["canError", "radarFault"]  # 固定顺序，非空才出现
    assert snap["leadTwo"]["status"] is False  # 无目标也是合法段
    json.dumps(snap)

  def test_defaults_readable(self):
    # 全默认（从未 update 过的 RadarState，offroad 首帧）不能抛
    snap = radar_snapshot(log.RadarState.new_message())
    assert snap["leadOne"]["status"] is False and snap["leadOne"]["trackId"] == -1
    assert snap["leadsCenterCount"] == 0 and snap["radarErrors"] == []


class TestFramePayload:
  def test_segments(self):
    car = {"vEgo": 10.0}
    radar = {"leadOne": {"status": True}}
    carrot = {"desiredSpeed": 60}
    assert frame_payload(car, radar, carrot) == {"car": car, "radar": radar, "carrot": carrot}
    assert frame_payload(car, None) == {"car": car}          # 单段出现
    assert frame_payload(None, radar) == {"radar": radar}
    assert frame_payload(None, None, carrot) == {"carrot": carrot}  # v4: carrot 段独立出现
    assert frame_payload(None, None) is None                 # 窗口全空不出帧


class TestFrameTicker:
  def test_window_accumulates_and_emits_once(self):
    t = FrameTicker(FRAME_PERIOD_S, now=100.0)
    assert t.tick(100.4) is None              # 未到点，持续积累窗口
    t.car = {"vEgo": 10.0}
    t.radar = {"leadOne": {"status": True}}
    t.carrot = {"desiredSpeed": 60}
    p = t.tick(100.5)                          # 到点出帧
    assert p == {"car": {"vEgo": 10.0}, "radar": {"leadOne": {"status": True}}, "carrot": {"desiredSpeed": 60}}
    assert t.tick(100.6) is None               # 出帧后窗口清空，下个节拍重新积累

  def test_no_update_skips_frame(self):
    # offroad（数据源停止）：到点但窗口无更新 → 不出帧，自然停推
    t = FrameTicker(FRAME_PERIOD_S, now=100.0)
    assert t.tick(100.5) is None
    assert t.tick(101.0) is None               # 下个节拍仍无更新

  def test_single_segment_only(self):
    t = FrameTicker(FRAME_PERIOD_S, now=100.0)
    t.radar = {"leadOne": {"status": False}}
    assert t.tick(100.5) == {"radar": {"leadOne": {"status": False}}}

  def test_carrot_segment_only(self):
    t = FrameTicker(FRAME_PERIOD_S, now=100.0)
    t.carrot = carrot_snapshot(custom.CarrotMan.new_message(
      nRoadLimitSpeed=100, xSpdLimit=60, xSpdDist=350, desiredSpeed=60,
      desiredSource="sdi", szPosRoadName="G4", leftSec=5, trafficState=2))
    p = t.tick(100.5)
    assert p["carrot"]["nRoadLimitSpeed"] == 100
    assert p["carrot"]["desiredSource"] == "sdi"
    assert p["carrot"]["szPosRoadName"] == "G4"
    assert set(p) == {"carrot"}

  def test_lag_catches_up_without_burst(self):
    # 轮询阻塞落后多个周期：追一个节拍（不积压连发），窗口照常清空
    t = FrameTicker(FRAME_PERIOD_S, now=100.0)
    t.car = {"vEgo": 1.0}
    assert t.tick(103.0) == {"car": {"vEgo": 1.0}}
    assert t.next_t == 103.5                   # now + period，而非连追三个周期
    assert t.car is None and t.radar is None and t.carrot is None


# ---------------- v5 快照段 ----------------

class TestDeviceSnapshots:
  def test_device_snapshot(self):
    ds = log.DeviceState.new_message(
      networkType="cell4G", networkStrength="good", networkMetered=True, started=True,
      freeSpacePercent=33.3, memoryUsagePercent=61, gpuUsagePercent=20,
      cpuUsagePercent=[10, 20, 30], powerDrawW=5.678, somPowerDrawW=3.2,
      cpuTempC=[45.6, 46.1], memoryTempC=44.5, maxTempC=46.1,
      thermalZones=[{"name": "cpu0", "temp": 45.6}],
      thermalStatus="warmDEPRECATED", fanSpeedPercentDesired=30,
    )
    d = device_snapshot(ds)
    assert d["networkType"] == "cell4G" and d["networkStrength"] == "good"
    assert d["started"] is True and d["networkMetered"] is True
    assert d["freeSpacePercent"] == 33.3 and d["memoryUsagePercent"] == 61
    assert d["cpuUsagePercent"] == [10.0, 20.0, 30.0]
    assert d["powerDrawW"] == 5.68 and d["cpuTempC"] == [45.6, 46.1]
    assert d["thermalZones"] == [{"name": "cpu0", "tempC": 45.6}]
    assert d["thermalStatus"] == "warmDEPRECATED"
    assert d["fanSpeedPercentDesired"] == 30
    json.dumps(d)  # 必须始终可序列化

  def test_device_snapshot_defaults_readable(self):
    d = device_snapshot(log.DeviceState.new_message())
    assert d["networkType"] == "none" and d["thermalStatus"] == "ok"
    assert d["cpuUsagePercent"] == [] and d["thermalZones"] == []
    json.dumps(d)

  def test_peripheral_snapshot(self):
    p = peripheral_snapshot(log.PeripheralState.new_message(
      pandaType="dos", voltage=5000, current=1500, fanSpeedRpm=3200))
    assert p == {"pandaType": "dos", "voltage": 5000, "current": 1500, "fanSpeedRpm": 3200}
    assert peripheral_snapshot(log.PeripheralState.new_message())["pandaType"] == "unknown"

  def test_manager_snapshot(self):
    ms = log.ManagerState.new_message(processes=[
      {"name": "card", "pid": 123, "running": True, "shouldBeRunning": True},
      {"name": "modeld", "pid": 124, "running": False, "shouldBeRunning": False, "exitCode": 1},
    ])
    d = manager_snapshot(ms)
    assert d["processes"][0] == {"name": "card", "pid": 123, "running": True, "shouldBeRunning": True, "exitCode": 0}
    assert d["processes"][1]["exitCode"] == 1 and d["processes"][1]["running"] is False
    json.dumps(d)

  def test_proc_snapshot(self):
    pl = log.ProcLog.new_message(
      mem={"total": 8 * 1048576, "free": 2 * 1048576, "available": 3 * 1048576},
      procs=[{"name": "card", "pid": 1, "cpuUser": 10.567, "cpuSystem": 2.1,
              "numThreads": 5, "memRss": 1048576, "memPss": 900000}],
    )
    d = proc_snapshot(pl)
    assert d["mem"] == {"totalMb": 8.0, "freeMb": 2.0, "availableMb": 3.0}
    p = d["procs"][0]
    assert p["name"] == "card" and p["cpuUser"] == 10.57 and p["cpuSystem"] == 2.1
    assert p["rssMb"] == 1.0 and p["threads"] == 5
    json.dumps(proc_snapshot(log.ProcLog.new_message()))  # 全默认可读

  def test_backup_snapshot(self):
    bm = custom.BackupManagerSP.new_message(
      backupStatus="inProgress", restoreStatus="idle", backupProgress=0.5,
      lastError="disk full", currentBackup={"deviceId": "dev1", "version": 3, "isEncrypted": True,
                                            "createdAt": "2026-11-08T00:00:00Z"},
    )
    d = backup_snapshot(bm)
    assert d["backupStatus"] == "inProgress" and d["restoreStatus"] == "idle"
    assert d["backupProgress"] == 0.5 and d["lastError"] == "disk full"
    assert d["currentBackup"]["deviceId"] == "dev1" and d["currentBackup"]["version"] == 3
    assert d["currentBackup"]["isEncrypted"] is True
    json.dumps(backup_snapshot(custom.BackupManagerSP.new_message()))  # 全默认可读

  def test_temp_snapshot(self):
    e = log.SensorEventData.new_message(timestamp=12345, source="lsm6ds3trc", temperature=30.56)
    d = temp_snapshot(e)
    assert d == {"timestamp": 12345, "source": "lsm6ds3trc", "temperatureC": 30.56}
    # temperature 分支未选中：pycapnp 读取会抛，兜底为 None（客户端判读为无效）
    assert temp_snapshot(log.SensorEventData.new_message())["temperatureC"] is None
    json.dumps(d)


class TestCarControlSnapshots:
  def test_car_params_snapshot(self):
    cp = car.CarParams.new_message(
      brand="byd", carFingerprint="SONG PLUS DM-i", carVin="VIN123", pcmCruise=False,
      flags=8, minSteerSpeed=5.0, wheelbase=2.76, steerRatio=15.2, steerControlType="torque",
      openpilotLongitudinalControl=True, transmissionType="direct", radarUnavailable=True,
      networkLocation="fwdCamera",
    )
    cps = custom.CarParamsSP.new_message(flags=1, safetyParam=2, pcmCruiseSpeed=True,
                                         enableGasInterceptor=False)
    d = car_params_snapshot(cp, cps)
    assert d["brand"] == "byd" and d["carFingerprint"] == "SONG PLUS DM-i"
    assert d["steerControlType"] == "torque" and d["transmissionType"] == "direct"
    assert d["networkLocation"] == "fwdCamera" and d["openpilotLongitudinalControl"] is True
    assert d["wheelbase"] == 2.76 and d["steerRatio"] == 15.2
    assert d["sp"] == {"flags": 1, "safetyParam": 2, "pcmCruiseSpeed": True,
                       "intelligentCruiseButtonManagementAvailable": False, "enableGasInterceptor": False}
    json.dumps(car_params_snapshot(car.CarParams.new_message(), custom.CarParamsSP.new_message()))

  def test_controls_snapshot(self):
    # 本仓 ControlsState 的 state/alert 字段在 deprecated group 下（carrot 树遗留布局）
    cs = log.ControlsState.new_message(
      deprecated={"state": "enabled", "enabled": True, "active": True,
                  "alertText1": "BRAKE!", "alertText2": "Risk of Collision", "alertType": "fcw",
                  "alertStatus": "critical", "alertSize": "full", "alertSound2": "prompt",
                  "decelForTurn": True},
      longControlState="pid", curvature=0.01234, desiredCurvature=0.01235, forceDecel=False,
    )
    d = controls_snapshot(cs)
    assert d["state"] == "enabled" and d["enabled"] is True and d["active"] is True
    assert d["alertText1"] == "BRAKE!" and d["alertType"] == "fcw"
    assert d["alertStatus"] == "critical" and d["alertSize"] == "full"
    assert d["alertSound"] == "prompt" and d["longControlState"] == "pid"
    assert d["curvature"] == 0.01234 and d["decelForTurn"] is True
    json.dumps(controls_snapshot(log.ControlsState.new_message()))  # 全默认可读

  def test_car_sp_snapshot(self):
    assert car_sp_snapshot(custom.CarStateSP.new_message(speedLimit=13.89)) == {"speedLimit": 13.89}
    assert car_sp_snapshot(custom.CarStateSP.new_message()) == {"speedLimit": 0.0}

  def test_control_snapshot(self):
    cc = car.CarControl.new_message(
      enabled=True, latActive=True, longActive=True, leftBlinker=True,
      currentCurvature=0.01,
      actuators={"torque": 0.52, "steeringAngleDeg": 12.3, "accel": -0.4, "gas": 0.2,
                 "brake": 0.0, "longControlState": "pid", "torqueOutputCan": 256.5, "speed": 12.0},
      hudControl={"setSpeed": 60.0, "visualAlert": "steerRequired", "leadDistanceBars": 2,
                  "leadVisible": True, "leftLaneVisible": True},
      cruiseControl={"resume": False, "override": True},
    )
    ccsp = custom.CarControlSP.new_message(mads={"enabled": True, "active": True,
                                                 "available": True, "state": "enabled"})
    d = control_snapshot(cc, ccsp)
    assert d["enabled"] is True and d["latActive"] is True and d["leftBlinker"] is True
    a = d["actuators"]
    assert a["torque"] == 0.52 and a["gas"] == 0.2 and a["torqueOutputCan"] == 256.5
    assert a["longControlState"] == "pid"
    assert d["hud"]["setSpeed"] == 60.0 and d["hud"]["visualAlert"] == "steerRequired"
    assert d["hud"]["leadDistanceBars"] == 2 and d["hud"]["leadVisible"] is True
    assert d["cruise"] == {"cancel": False, "resume": False, "override": True}
    assert d["mads"] == {"enabled": True, "active": True, "available": True, "state": "enabled"}
    json.dumps(control_snapshot(car.CarControl.new_message(), custom.CarControlSP.new_message()))

  def test_plan_snapshot(self):
    lp = log.LongitudinalPlan.new_message(
      hasLead=True, fcw=False, longitudinalPlanSource="lead0", aTarget=-0.52,
      shouldStop=False, allowThrottle=False, allowBrake=True, accelBoost=0.1,
      accels=[0.1, -0.2], speeds=[20.0, 19.8], jerks=[-0.1, -0.1], processingDelay=0.012,
    )
    lpsp = custom.LongitudinalPlanSP.new_message(
      longitudinalPlanSource="speedLimitAssist", vTarget=24.0, aTarget=-0.3,
      smartCruiseControl={"vision": {"state": "entering", "enabled": True, "active": True,
                                    "vTarget": 18.0, "aTarget": -0.5,
                                    "currentLateralAccel": 1.2, "maxPredictedLateralAccel": 2.5},
                          "map": {"state": "enabled"}},
      speedLimit={"resolver": {"speedLimit": 25.0, "distToSpeedLimit": 300.0, "source": "map",
                               "speedLimitOffset": 5.0, "speedLimitFinal": 30.0, "speedLimitValid": True},
                  "assist": {"state": "adapting", "enabled": True, "active": True,
                             "vTarget": 25.0, "aTarget": -0.8}},
      e2eAlerts={"greenLightAlert": True},
    )
    lmp = log.LateralManeuverPlan.new_message(desiredCurvature=0.01234)
    d = plan_snapshot(lp, lpsp, lmp)
    assert d["hasLead"] is True and d["source"] == "lead0"
    assert d["aTarget"] == -0.52 and d["allowBrake"] is True
    assert d["accels"] == [0.1, -0.2] and d["speeds"] == [20.0, 19.8] and d["jerks"] == [-0.1, -0.1]
    assert d["desiredCurvature"] == 0.01234
    sp = d["sp"]
    assert sp["source"] == "speedLimitAssist" and sp["vTarget"] == 24.0
    assert sp["sccVision"]["state"] == "entering" and sp["sccVision"]["maxPredictedLateralAccel"] == 2.5
    assert sp["sccMap"]["state"] == "enabled"
    assert sp["speedLimit"]["speed"] == 25.0 and sp["speedLimit"]["source"] == "map"
    assert sp["speedLimit"]["final"] == 30.0 and sp["speedLimit"]["assistState"] == "adapting"
    assert sp["e2eAlerts"] == {"greenLight": True, "leadDepart": False}
    json.dumps(plan_snapshot(log.LongitudinalPlan.new_message(),
                             custom.LongitudinalPlanSP.new_message(),
                             log.LateralManeuverPlan.new_message()))


class TestLocGpsMapSnapshots:
  def test_loc_snapshot(self):
    llk = log.LiveLocationKalman.new_message(
      status="valid", unixTimestampMillis=1791472951467, gpsWeek=2380, gpsTimeOfWeek=123.45,
      inputsOK=True, gpsOK=True, sensorsOK=True, deviceStable=True,
      timeSinceReset=12.3, timeToFirstFix=1.5,
      positionGeodetic={"value": [31.2345678, 121.4567890, 10.0], "std": [1.0, 1.0, 2.0], "valid": True},
      velocityNED={"value": [10.0, 0.1, 0.0], "std": [0.1, 0.1, 0.1], "valid": True},
      orientationNED={"value": [0.0, 0.0, 1.57], "std": [0.01, 0.01, 0.02], "valid": True},
    )
    d = loc_snapshot(llk)
    assert d["status"] == "valid" and d["unixTimestampMillis"] == 1791472951467
    assert d["posGeodetic"]["value"] == [31.2345678, 121.456789, 10.0]
    assert d["posGeodetic"]["valid"] is True
    assert d["velNED"]["value"] == [10.0, 0.1, 0.0]
    assert d["orientationNED"]["value"] == [0.0, 0.0, 1.57]
    assert d["gpsOK"] is True and d["timeToFirstFix"] == 1.5
    json.dumps(loc_snapshot(log.LiveLocationKalman.new_message()))  # 全默认可读

  def test_gps_snapshot(self):
    g = log.GpsLocationData.new_message(
      latitude=31.2345678, longitude=121.4567890, altitude=10.5, speed=10.2, bearingDeg=90.5,
      horizontalAccuracy=2.0, verticalAccuracy=4.0, bearingAccuracyDeg=1.0, speedAccuracy=0.5,
      unixTimestampMillis=1791472951000, source="ublox", vNED=[10.2, 0.0, 0.0],
      hasFix=True, satelliteCount=12,
    )
    d = gps_snapshot(g)
    assert d["latitude"] == 31.2345678 and d["longitude"] == 121.456789
    assert d["speed"] == 10.2 and d["bearingDeg"] == 90.5
    assert d["source"] == "ublox" and d["hasFix"] is True and d["satelliteCount"] == 12
    assert d["vNED"] == [10.2, 0.0, 0.0]
    json.dumps(gps_snapshot(log.GpsLocationData.new_message()))

  def test_map_snapshot(self):
    lmd = custom.LiveMapDataSP.new_message(
      speedLimitValid=True, speedLimit=25.0, speedLimitAheadValid=True, speedLimitAhead=30.0,
      speedLimitAheadDistance=500.5, roadName="G4 京港澳高速")
    d = map_snapshot(lmd)
    assert d["speedLimitValid"] is True and d["speedLimit"] == 25.0
    assert d["speedLimitAhead"] == 30.0 and d["speedLimitAheadDistance"] == 500.5
    assert d["roadName"] == "G4 京港澳高速"
    json.dumps(map_snapshot(custom.LiveMapDataSP.new_message()))


class TestModelSnapshots:
  def test_model_snapshot(self):
    m = log.ModelDataV2.new_message(
      frameId=100, frameDropPerc=0.0, modelExecutionTime=0.008, big=True, confidence="green",
      meta={"engagedProb": 0.95, "hardBrakePredicted": False,
            "laneChangeState": "laneChangeStarting", "laneChangeDirection": "left"},
      action={"desiredCurvature": 0.01234, "desiredAcceleration": -0.2, "shouldStop": False},
      laneLineProbs=[0.9, 0.8, 0.7, 0.6], laneLineStds=[0.1, 0.2, 0.2, 0.3],
      leadsV3=[{"prob": 0.92, "probTime": 0.0, "t": [0.0, 0.1], "x": [30.5, 31.0],
                "y": [-1.2, -1.2], "v": [25.0, 25.1], "a": [-1.0, -1.0]}],
    )
    msp = custom.ModelDataV2SP.new_message(
      laneTurnDirection="turnLeft", leftLaneChangeEdgeBlock=False, rightLaneChangeEdgeBlock=True,
      modelCurvature=0.012, blendfulCurvature=0.013, blendfulTrust=0.8,
      blendfulGoingAround=False, blendfulLeftProb=0.9, blendfulRightProb=0.1, blendfulLaneWidth=3.5)
    d = model_snapshot(m, msp)
    assert d["frameId"] == 100 and d["big"] is True and d["confidence"] == "green"
    assert d["engagedProb"] == 0.95 and d["hardBrakePredicted"] is False
    assert d["laneChangeState"] == "laneChangeStarting" and d["laneChangeDirection"] == "left"
    assert d["action"] == {"desiredCurvature": 0.01234, "desiredAcceleration": -0.2, "shouldStop": False}
    assert d["laneLineProbs"] == [0.9, 0.8, 0.7, 0.6]
    # leads 只留 t=0 首采样
    assert d["leads"] == [{"prob": 0.92, "probTime": 0.0, "t": 0.0, "x": 30.5, "y": -1.2, "v": 25.0, "a": -1.0}]
    assert d["sp"]["laneTurnDirection"] == "turnLeft" and d["sp"]["rightLaneChangeEdgeBlock"] is True
    assert d["sp"]["modelCurvature"] == 0.012 and d["sp"]["blendfulTrust"] == 0.8
    json.dumps(model_snapshot(log.ModelDataV2.new_message(), custom.ModelDataV2SP.new_message()))

  def test_dmodel_snapshot(self):
    dm = log.DrivingModelData.new_message(
      frameId=200, modelExecutionTime=0.007, big=False,
      action={"desiredCurvature": 0.01, "desiredAcceleration": 0.0, "shouldStop": False},
      laneLineMeta={"leftY": -1.8, "rightY": 1.9, "leftProb": 0.92, "rightProb": 0.88},
      meta={"laneChangeState": "off"},
      path={"xCoefficients": [0.0, 0.0, 0.0, 0.0], "yCoefficients": [0.1, 0.2, 0.3, 0.4],
            "zCoefficients": [0.0, 0.0, 0.0, 0.0]},
    )
    d = dmodel_snapshot(dm)
    assert d["frameId"] == 200 and d["big"] is False
    assert d["laneLineMeta"] == {"leftY": -1.8, "rightY": 1.9, "leftProb": 0.92, "rightProb": 0.88}
    assert d["laneChangeState"] == "off"
    assert d["path"]["y"] == [0.1, 0.2, 0.3, 0.4]
    assert d["action"]["desiredCurvature"] == 0.01
    json.dumps(dmodel_snapshot(log.DrivingModelData.new_message()))

  def test_odom_snapshot(self):
    co = log.CameraOdometry.new_message(
      frameId=300, trans=[10.2, 0.1, 0.0], rot=[0.0, 0.0, 0.012],
      transStd=[0.1, 0.1, 0.1], rotStd=[0.01, 0.01, 0.01],
      wideFromDeviceEuler=[0.0, 0.0, 0.01], wideFromDeviceEulerStd=[0.01, 0.01, 0.01])
    d = odom_snapshot(co)
    assert d["frameId"] == 300
    assert d["trans"] == [10.2, 0.1, 0.0] and d["rot"] == [0.0, 0.0, 0.012]
    assert d["transStd"] == [0.1, 0.1, 0.1]
    json.dumps(odom_snapshot(log.CameraOdometry.new_message()))


class TestCalibParamsSnapshots:
  def test_calib_snapshot(self):
    ec = log.ExtrinsicsCalibration.new_message(
      calStatus="calibrated", calPerc=100, calCycle=5, validBlocks=20,
      rpyCalib=[0.0, -0.02, 0.123], rpyCalibSpread=[0.001, 0.001, 0.002],
      height=[0.5], wideFromDeviceEuler=[0.0, 0.0, 0.01],
      extrinsicMatrix=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0])
    d = calib_snapshot(ec)
    assert d["status"] == "calibrated" and d["calPerc"] == 100 and d["validBlocks"] == 20
    assert d["rpyCalib"] == [0.0, -0.02, 0.123]
    assert d["extrinsicMatrix"][0] == 1.0 and len(d["extrinsicMatrix"]) == 9
    json.dumps(calib_snapshot(log.ExtrinsicsCalibration.new_message()))

  def test_params_snapshot(self):
    ltp = log.LateralTorqueParameters.new_message(
      valid=True, useParams=True, calPerc=80,
      latAccelFactorRaw=8.5, latAccelOffsetRaw=0.1, frictionCoefficientRaw=0.06,
      latAccelFactorFiltered=8.4, latAccelOffsetFiltered=0.12, frictionCoefficientFiltered=0.07)
    ld = log.LateralDelay.new_message(lateralDelay=0.24, lateralDelayEstimate=0.25,
                                      lateralDelayEstimateStd=0.01, validBlocks=15,
                                      status="estimated")
    vp = log.VehicleParameters.new_message(valid=True, steerRatio=15.2, steerRatioValid=True,
                                           stiffnessFactor=1.05, stiffnessFactorValid=True,
                                           angleOffsetDeg=0.1, angleOffsetAverageDeg=0.05,
                                           angleOffsetAverageValid=True, roll=0.001)
    d = params_snapshot(ltp, ld, vp)
    assert d["latTorque"]["latAccelFactorFiltered"] == 8.4 and d["latTorque"]["useParams"] is True
    assert d["lateralDelay"] == {"lateralDelay": 0.24, "estimate": 0.25, "estimateStd": 0.01,
                                 "validBlocks": 15, "status": "estimated"}
    assert d["vehicle"]["steerRatio"] == 15.2 and d["vehicle"]["stiffnessFactor"] == 1.05
    assert d["vehicle"]["angleOffsetAverageDeg"] == 0.05 and d["vehicle"]["roll"] == 0.001
    json.dumps(params_snapshot(log.LateralTorqueParameters.new_message(),
                               log.LateralDelay.new_message(),
                               log.VehicleParameters.new_message()))

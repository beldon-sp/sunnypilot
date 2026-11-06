import json
import time

from openpilot.cereal import car, custom, log
from openpilot.mate.poller import (
  EVENT_FLAGS,
  FRAME_PERIOD_S,
  FrameTicker,
  SoundTracker,
  button_payloads,
  carrot_snapshot,
  car_snapshot,
  diff_events,
  enum_name,
  event_to_dict,
  frame_payload,
  opstate_payload,
  panda_payload,
  radar_snapshot,
  services_status,
  sound_payload,
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

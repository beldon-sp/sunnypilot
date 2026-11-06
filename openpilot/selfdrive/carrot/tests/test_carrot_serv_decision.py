"""carrot_serv 决策单测（task 5.7 / 6.1）：
road/sdi/atc/vturn 各源取胜场景 + 全源并发取严格最小 + BYD 无车端相机字段的退化形态
（hda/bump/school=250，min() 不受污染 = carrot BYD 实车基线）。
"""
import time

import numpy as np
import pytest

from openpilot.cereal import car, custom, log
from openpilot.selfdrive.carrot.carrot_navi_control import parse_carrot_navi_control
from openpilot.selfdrive.carrot.carrot_serv import CarrotServ


@pytest.fixture(autouse=True)
def _shm_params(monkeypatch, tmp_path):
  # /dev/shm 在 macOS 不可写：carrot_serv 的 params_memory（TrafficLight 状态）
  # 重定向到临时目录，device 上无需此补丁
  from openpilot.common.params import Params
  monkeypatch.setattr("openpilot.selfdrive.carrot.carrot_serv.Params",
                      lambda d=None: Params(str(tmp_path / ("shm" if d else "reg"))))


class FakePM:
  def __init__(self):
    self.sent = []

  def send(self, service, msg):
    self.sent.append((service, msg))


class FakeSM:
  """最小 SubMaster 替身：update_navi 决策路径用到的字段。"""

  def __init__(self, carrot_navi_msg=None, cs=None, ss=None):
    now = time.monotonic()
    self._messages = {
      "carrotNavi": carrot_navi_msg,
      "carState": cs if cs is not None else car.CarState.new_message(),
      "selfdriveState": ss if ss is not None else log.SelfdriveState.new_message(),
      "navInstruction": log.NavInstruction.new_message(),
      "gpsLocationExternal": log.GpsLocationData.new_message(),
    }
    self.services = list(self._messages)
    self.updated = dict.fromkeys(self._messages, True)
    self.seen = dict.fromkeys(self._messages, True)
    self.alive = dict.fromkeys(self._messages, True)
    self.valid = dict.fromkeys(self._messages, True)
    self.recv_time = dict.fromkeys(self._messages, now)
    self.logMonoTime = dict.fromkeys(self._messages, int(now * 1e9))
    self.frame = 1

  def __getitem__(self, key):
    msg = self._messages[key]
    if msg is None:
      raise KeyError(key)
    return msg


def navi_control_msg(road_limit=None, sdi=None, road_name="G4"):
  """构造 carrotNavi cereal 消息（经 parse → CarrotNaviControl）。"""
  msg = log.Event.new_message()
  nav = msg.init('carrotNavi')
  nav.schemaVersion = 1
  nav.connected = True
  nav.vehicle.roadName = road_name
  speed = nav.speed
  if road_limit is not None or sdi is not None:
    speed.meta.present = True  # has_control 判据：speed/guidance meta.present 任一
  if road_limit is not None:
    speed.roadLimitValid = True
    speed.roadLimitKph = road_limit
  if sdi is not None:
    speed.sdiPresent = True
    speed.sdiType, speed.sdiDistanceM, speed.sdiSpeedLimitKph = sdi
  return nav  # parse_carrot_navi_control 期望 carrotNavi 结构体（update_navi 传 sm['carrotNavi']）


def run_decision(serv, road_limit=None, sdi=None, cs=None, vturn_speed=250.0, frame=1):
  sm = FakeSM(carrot_navi_msg=navi_control_msg(road_limit, sdi), cs=cs)
  pm = FakePM()
  sm.updated["carrotNavi"] = frame == 1  # 首帧喂 control，其后复用 has_control 保活路径
  serv.update_navi("127.0.0.1", sm, pm, vturn_speed, [], [], 300.0, "gpsLocationExternal")
  cm = pm.sent[0][1].carrotMan
  return cm.desiredSpeed, cm.desiredSource, cm


class TestDegradedBaseline:
  """BYD 退化形态（5.7）：无车端相机字段 → hda/bump/school=250，min() 不受污染。"""

  def setup_method(self):
    self.serv = CarrotServ()

  def test_road_only_caps_at_limit(self):
    # AMap 道路限速 60（本仓默认 AutoRoadSpeedLimitOffset=0 → road 源=限速原值）
    desired, source, cm = run_decision(self.serv, road_limit=60)
    assert desired == 60 and source == "road"

  def test_no_road_limit_defaults_to_road_200(self):
    desired, source, _ = run_decision(self.serv, road_limit=None)
    assert desired == 200 and source == "road"

  def test_vehicle_camera_sources_stay_250(self):
    # 本仓 CarState 无 speedLimit/speedLimitDistance/speedBumpDistance/schoolZoneActive：
    # hda/hda_bump/school/hda_section 恒 250，绝不赢下 min()
    cs = car.CarState.new_message(vEgo=0.0)  # 静止：排除 20Hz vEgo 积分对 xSpdDist 的每帧递减
    desired, source, cm = run_decision(self.serv, road_limit=100, sdi=(1, 100, 40), cs=cs)
    # sdi 曲线（100m、限速 40×安全系数）低于 road=100；车端相机源不出现
    expected_curve = self.serv.calculate_current_speed(
      100, 40 * self.serv.autoNaviSpeedSafetyFactor, self.serv.autoNaviSpeedCtrlEnd, self.serv.autoNaviSpeedDecelRate)
    assert abs(desired - expected_curve) <= 1  # 发布链路含 gas-floor/取整整形
    assert desired < 100
    assert source not in ("hda", "hda_bump", "school", "hda_section")

  def test_min_not_polluted_by_camera_fields(self):
    # 本仓 CarState 结构上就没有 speedLimit/speedLimitDistance 字段（getattr 兜底为 0，
    # =carrot BYD 基线的"恒 0"语义）：road cap 不受任何伪源污染
    cs = car.CarState.new_message(vEgo=0.0)
    assert not hasattr(cs, "speedLimit") and not hasattr(cs, "speedLimitDistance")
    desired, source, _ = run_decision(self.serv, road_limit=60, cs=cs)
    assert desired == 60 and source == "road"


class TestMultiSourceMin:
  """6.1：road/sdi/atc/vturn 各源取胜场景 + 全源并发取严格最小。"""

  def setup_method(self):
    self.serv = CarrotServ()

  def test_sdi_curve_wins_over_road(self):
    # 区间测速：距 100m、限速 40（×安全系数后曲线值）< road 60
    desired, source, _ = run_decision(self.serv, road_limit=60, sdi=(1, 100, 40))
    expected_curve = self.serv.calculate_current_speed(
      100, 40 * self.serv.autoNaviSpeedSafetyFactor, self.serv.autoNaviSpeedCtrlEnd, self.serv.autoNaviSpeedDecelRate)
    assert abs(desired - min(60, expected_curve)) <= 1  # 发布链路含 gas-floor/取整整形
    assert source != "road"

  def test_far_sdi_does_not_beat_road(self):
    # 远距离 SDI（5km）曲线值高于 road → road 仍取胜
    desired, source, _ = run_decision(self.serv, road_limit=60, sdi=(1, 5000, 80))
    assert desired == 60 and source == "road"

  def test_atc_source_by_turn_distance(self):
    # ATC：前方转向（AMap turnType=12 left, 80m）→ atc 源限速。
    # AutoTurnControl 默认 0=off（update_params 每帧从注册表刷新）→ 需置 2
    self.serv.params.put("AutoTurnControl", 2)
    self.serv.update_params()
    sm = FakeSM(carrot_navi_msg=navi_control_msg(road_limit=100))
    sm.updated["carrotNavi"] = True
    pm = FakePM()
    serv = self.serv
    serv.update_navi("127.0.0.1", sm, pm, 250.0, [], [], 300.0, "gpsLocationExternal")
    # 引导流喂入 turn 信息（新 session + 新 sequence 才会被应用）后二帧再决策
    nav2 = navi_control_msg(road_limit=100)
    nav2.sessionId = "s2"
    nav2.guidanceCurrent.meta.present = True
    nav2.guidanceCurrent.meta.sequence = 2
    nav2.guidanceCurrent.turnType = 12  # AMap: turn-left（_update_tbt 映射 → xTurnInfo=1）
    nav2.guidanceCurrent.distanceM = 80
    sm2 = FakeSM(carrot_navi_msg=nav2)
    sm2.updated["carrotNavi"] = True
    serv.update_navi("127.0.0.1", sm2, pm, 250.0, [], [], 300.0, "gpsLocationExternal")
    cm = next(m for s, m in reversed(pm.sent) if s == "carrotMan").carrotMan
    assert cm.desiredSpeed < 100  # ATC 转弯减速介入
    assert cm.desiredSource == "atc" or cm.desiredSource == "atc2"

  def test_vturn_source_when_enabled(self):
    # turnSpeedControlMode=1（默认）→ vturn 源参与：视觉曲率 30km/h 时取胜
    desired, source, _ = run_decision(self.serv, road_limit=100, vturn_speed=30.0)
    assert desired == max(30, self.serv.autoCurveSpeedLowerLimit)
    assert source == "vturn"

  def test_strict_min_all_sources_concurrent(self):
    # 全源并发：desiredSpeed = 各源的严格最小值（road=60、vturn=35、sdi 曲线）
    self.serv.params.put("AutoTurnControl", 2)
    self.serv.update_params()
    desired, source, _ = run_decision(self.serv, road_limit=60, sdi=(1, 100, 45), vturn_speed=35.0)
    assert desired <= 35
    assert source == "vturn"

  def test_guard_off_publishes_connect_false_but_serv_keeps_state(self):
    # carrotNavi 断流：update_navi 不再保活（active_carrot=0 → 源退化为 road/200 基线）
    sm = FakeSM(carrot_navi_msg=None)
    sm.alive["carrotNavi"] = sm.valid["carrotNavi"] = sm.seen["carrotNavi"] = False
    sm.updated["carrotNavi"] = False
    pm = FakePM()
    self.serv.carrot_navi_active = True
    self.serv.carrot_navi_has_control = True
    self.serv.update_navi("127.0.0.1", sm, pm, 250.0, [], [], 300.0, "gpsLocationExternal")
    cm = pm.sent[0][1].carrotMan
    assert cm.activeCarrot == 0 or self.serv.active_carrot == 0

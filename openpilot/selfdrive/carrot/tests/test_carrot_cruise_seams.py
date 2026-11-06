"""carrot 接缝语义重写测试（task 6.3，照 test_cruise_byd.py 先例）：
cruise cap（guard 断即放开）、SLA gate（模式期冻结/退出重新武装）、lateral 切换（ATC 冻结变道）。
仲裁滞回本体（进入/退出/抖振/行驶中切换）见 test_carrot_mode.py（6.2）。
"""
import time

import pytest

from openpilot.selfdrive.carrot.carrot_cruise import CarrotCruise, CRUISE_MAX_VALS_CARROT, A_CRUISE_MAX_BP_CARROT
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper
from opendbc.car.structs import car


class FakeCarrotMan:
  def __init__(self, desired_speed=60, road_limit=60, active_carrot=2, dist_to_turn=0):
    self.desiredSpeed = desired_speed
    self.nRoadLimitSpeed = road_limit
    self.activeCarrot = active_carrot
    self.xDistToTurn = dist_to_turn
    self.desiredSource = "road"
    self.trafficState = 0


class FakeSM:
  """get_carrot_man 契约的最小替身：None = 没数据；recv_age 模拟消息时效。"""

  def __init__(self, cm=None, recv_age=0.0):
    now = time.monotonic()
    self.seen = {"carrotMan": cm is not None}
    self.alive = {"carrotMan": cm is not None}
    self.valid = {"carrotMan": cm is not None}
    self.recv_time = {"carrotMan": now - recv_age}
    self._cm = cm

  def __getitem__(self, key):
    return self._cm


def fresh_cruise(enter_s=3.0, exit_s=10.0):
  from openpilot.common.params import Params
  from openpilot.selfdrive.carrot.carrot_mode import CarrotMode
  import tempfile
  c = CarrotCruise(Params(tempfile.mkdtemp()))
  c.mode = CarrotMode(enter_s=enter_s, exit_s=exit_s)  # 时钟注入便于测试
  return c


class TestCruiseCap:
  """纵向 cap：guard∧mode 才 cap；guard 断（≤1s）立即放开——即使滞回模式仍在宽限期内。"""

  def test_cap_follows_guard_not_mode(self):
    c = fresh_cruise(enter_s=0.0, exit_s=100.0)
    # guard 通 → mode 立即激活（enter_s=0 注入）
    assert c.update(FakeSM(FakeCarrotMan(desired_speed=60))) is True
    assert c.mode_active and c.capping

    # guard 断（recv_age=2s > 1s 超时）：mode 仍在 M 秒宽限内，但 cap 必须立即放开
    # （spec: 停发 1s 回落原生链路）
    c.update(FakeSM(None, recv_age=2.0))
    assert c.mode_active is True   # SLA/SCC 门与显示仍跟随模式
    assert c.capping is False      # 纵向 cap 已释放

  def test_cap_value_is_strict_min(self):
    # cap_v_target 单位 m/s：native 与 desiredSpeed 取严格最小
    c = fresh_cruise()
    c.capping = True
    c.desired_speed_kph = 60.0                       # carrot 60 km/h = 16.67 m/s
    assert c.cap_v_target(30.0) == pytest.approx(60 / 3.6)   # native 108 km/h → cap 到 60
    assert c.cap_v_target(10.0) == 10.0                       # native 36 km/h 更低 → 不变
    c.capping = False
    assert c.cap_v_target(30.0) == 30.0                       # 未 cap → 直通

  def test_accel_envelope_below_accel_max(self):
    # carrot accel 包络：低速 1.6 → 高速 0.6 m/s²，全程低于 openpilot ACCEL_MAX=2.0
    c = fresh_cruise()
    from opendbc.car.interfaces import ACCEL_MAX
    for v_ego in (0.0, 10.0, 30.0, 60.0 / 3.6):
      assert CRUISE_MAX_VALS_CARROT[0] + 1e-9 >= c.get_carrot_accel(v_ego) >= CRUISE_MAX_VALS_CARROT[-1] - 1e-9
      assert c.get_carrot_accel(v_ego) < ACCEL_MAX
    assert c.get_carrot_accel(140.0 / 3.6) == pytest.approx(CRUISE_MAX_VALS_CARROT[-1])


class TestSLAGate:
  """SLA gate：carrot 模式期冻结（mode_active），退出后重新武装。"""

  def test_mode_gates_sla_and_rearms(self):
    c = fresh_cruise(enter_s=0.0, exit_s=0.5)
    assert c.update(FakeSM(FakeCarrotMan())) is True
    assert c.mode_active is True  # plannerd 据此跳过 sla.update_buttons

    # guard 断 + 连续 stale M 秒 → 模式退出 → SLA 重新武装
    c.update(FakeSM(None, recv_age=2.0))   # 首次观察失效：宽限开始
    assert c.mode_active is True
    time.sleep(0.6)
    c.update(FakeSM(None, recv_age=2.6))   # 连续 stale 0.6s ≥ M=0.5s → 退出
    assert c.mode_active is False


class TestLateralSwitch:
  """横向切换：ATC 区间（activeCarrot>1 ∧ 0<xDistToTurn<100）冻结变道发起；两源禁同效。"""

  def _drive_to_pre_lane_change(self, dh):
    dh.frame = 100
    dh.lane_change_state = 0  # off → preLaneChange needs blinker
    return dh

  def test_atc_active_freezes_lane_change(self):
    dh = DesireHelper()
    dh.frame = 100
    dh.lane_change_speed_floor_ms = 0.0
    # 左转变道（direction=left）+ 驾驶员 torque 发起（torque_applied 路径，
    # 不依赖 ALCA 的 params 驱动的 auto 模式）
    cs = car.CarState.new_message(leftBlinker=True, vEgo=30.0, steeringPressed=True, steeringTorque=1.0)
    dh.lane_change_state = 1  # preLaneChange
    dh.lane_change_direction = 1

    # carrot ATC 区间：不允许发起变道
    dh.carrot_atc_active = True
    dh.update(cs, True, 0.0)
    assert dh.lane_change_state == 1  # 仍 preLaneChange，未进入 laneChangeStarting

    # ATC 结束 → 变道放行
    dh.carrot_atc_active = False
    dh.update(cs, True, 0.0)
    assert dh.lane_change_state == 2  # laneChangeStarting

  def test_atc_flag_derived_from_carrot_man(self):
    c = fresh_cruise()
    c.update(FakeSM(FakeCarrotMan(active_carrot=2, dist_to_turn=80)))
    assert c.atc_active is True
    c.update(FakeSM(FakeCarrotMan(active_carrot=1, dist_to_turn=80)))
    assert c.atc_active is False  # activeCarrot==1：无数据源生效，不冻结
    c.update(FakeSM(FakeCarrotMan(active_carrot=2, dist_to_turn=200)))
    assert c.atc_active is False  # 弯道 >100m：未进 ATC 区间


class TestCruisePreset:
  """巡航预设：carrot 权威期 set 拨杆目标跟随 nRoadLimitSpeed（×安全系数/+偏移）。"""

  def test_preset_follows_road_limit_with_offset_zero(self):
    c = fresh_cruise()  # 本仓默认 AutoRoadSpeedLimitOffset=0
    c.mode_active = True
    c.n_road_limit_speed = 80
    assert c.preset_speed_kph(40.0) == 80.0

  def test_preset_with_safety_factor_when_offset_negative(self):
    c = fresh_cruise()
    c.auto_road_speed_limit_offset = -1
    c.mode_active = True
    c.n_road_limit_speed = 80
    assert c.preset_speed_kph(40.0) == pytest.approx(80 * 1.05)

  def test_preset_falls_back_when_inactive(self):
    c = fresh_cruise()
    c.mode_active = False
    c.n_road_limit_speed = 80
    assert c.preset_speed_kph(40.0) == 40.0

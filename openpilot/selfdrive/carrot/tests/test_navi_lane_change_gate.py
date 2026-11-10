"""导航变道许可闸门测试：fail-open 契约 + 三种抑制条件 + DesireHelper 接缝。

- fail-open：闸门未启用 / carrotmate 掉线 / 数据无效 → 恒放行（= 现有纯拨杆 ALC）
- 抑制：临近转向/匝道（<300m）、crossroad（<150m）、单车道，仅在数据新鲜时生效
- DesireHelper 接缝：navi_lc_allowed=False 时 preLaneChange 不得进入 laneChangeStarting
"""
import tempfile
import time
from types import SimpleNamespace

import pytest

from openpilot.common.params import Params
from openpilot.selfdrive.carrot.navi_lane_change_gate import (
  CARROT_NAVI_TIMEOUT,
  BLOCK_TURN_TYPES,
  NaviLaneChangeGate,
)
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper
from opendbc.car.structs import car


def _meta(present=True):
  return SimpleNamespace(present=present)


def _navi(connected=True, lane_count=3, lane_present=True,
          turn_type=-1, turn_distance=1000, guidance_present=True,
          crossroad_visible=False, crossroad_distance=0, crossroad_present=True):
  return SimpleNamespace(
    connected=connected,
    laneCurrent=SimpleNamespace(meta=_meta(lane_present), count=lane_count),
    guidanceCurrent=SimpleNamespace(meta=_meta(guidance_present), turnType=turn_type, distanceM=turn_distance),
    crossroad=SimpleNamespace(meta=_meta(crossroad_present), visible=crossroad_visible, distanceM=crossroad_distance),
  )


class FakeSM:
  """get_carrot_navi 契约的最小替身：None = 没数据；recv_age 模拟消息时效。"""

  def __init__(self, navi=None, recv_age=0.0, subscribed=True):
    now = time.monotonic()
    self.seen = {"carrotNavi": subscribed}
    self.alive = {"carrotNavi": subscribed and navi is not None}
    self.valid = {"carrotNavi": subscribed and navi is not None}
    self.recv_time = {"carrotNavi": now - recv_age}
    self._navi = navi

  def __getitem__(self, key):
    return self._navi


def fresh_gate():
  return NaviLaneChangeGate(Params(tempfile.mkdtemp()))


class TestFailOpen:
  """fail-open 契约：任何不可用路径都必须放行，行为退回纯拨杆 ALC。"""

  def test_disabled_gate_always_allows(self):
    gate = fresh_gate()
    assert gate.update(FakeSM(_navi())) is True
    assert gate.active is False

  def test_unsubscribed_service_allows(self):
    gate = fresh_gate()
    gate.enabled = True
    assert gate.update(FakeSM(subscribed=False)) is True
    assert gate.block_reason == "navi_unavailable"

  def test_stale_message_allows(self):
    # 2 Hz 服务：3 帧无新数据（约 1.5 s）即视为掉线
    gate = fresh_gate()
    gate.enabled = True
    assert gate.update(FakeSM(_navi(), recv_age=CARROT_NAVI_TIMEOUT + 0.1)) is True
    assert gate.block_reason == "navi_unavailable"

  def test_fresh_message_within_timeout(self):
    gate = fresh_gate()
    gate.enabled = True
    assert gate.update(FakeSM(_navi(), recv_age=CARROT_NAVI_TIMEOUT - 0.1)) is True
    assert gate.block_reason == ""

  def test_disconnected_carrotmate_allows(self):
    # carrotmate 断连但 publisher 心跳仍在发：不得进入抑制判定
    gate = fresh_gate()
    gate.enabled = True
    assert gate.update(FakeSM(_navi(connected=False))) is True
    assert gate.block_reason == "navi_unavailable"


class TestSuppression:
  """抑制条件：临近转向/匝道、crossroad、单车道；仅在数据新鲜时生效。"""

  def _enabled_gate(self):
    gate = fresh_gate()
    gate.enabled = True
    return gate

  def test_clean_state_allows(self):
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi())) is True
    assert gate.active is True
    assert gate.block_reason == ""

  def test_near_turn_blocks(self):
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(turn_type=13, turn_distance=299))) is False  # 右转
    assert gate.block_reason == "near_turn"

  def test_turn_distance_boundary(self):
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(turn_type=13, turn_distance=300))) is True   # 恰好 300m：放行
    assert gate.update(FakeSM(_navi(turn_type=13, turn_distance=0))) is True     # 0m 视为无效数据：放行

  def test_ramp_and_slight_turn_types_block(self):
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(turn_type=1000, turn_distance=200))) is False  # slight left
    assert gate.update(FakeSM(_navi(turn_type=1006, turn_distance=200))) is False  # off ramp left

  def test_non_turn_and_unknown_types_allow(self):
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(turn_type=51, turn_distance=50))) is True    # notification，group None
    assert gate.update(FakeSM(_navi(turn_type=999, turn_distance=50))) is True   # 未知编码 fail-open
    assert gate.update(FakeSM(_navi(turn_type=-1, turn_distance=50))) is True    # 无转向

  def test_near_crossroad_blocks(self):
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(crossroad_visible=True, crossroad_distance=149))) is False
    assert gate.block_reason == "near_crossroad"
    assert gate.update(FakeSM(_navi(crossroad_visible=True, crossroad_distance=150))) is True
    assert gate.update(FakeSM(_navi(crossroad_visible=False, crossroad_distance=50))) is True  # 不可见不抑制

  def test_single_lane_blocks_but_zero_count_allows(self):
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(lane_count=1))) is False
    assert gate.block_reason == "single_lane"
    assert gate.update(FakeSM(_navi(lane_count=0))) is True  # count=0 视为数据无效，fail-open

  def test_missing_item_meta_allows(self):
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(lane_present=False, guidance_present=False, crossroad_present=False))) is True


class TestBlockTurnTypesConsistency:
  """BLOCK_TURN_TYPES 必须与 carrot_serv.nav_type_mapping group 1-5 同步。"""

  def test_matches_nav_type_mapping_groups(self):
    from openpilot.selfdrive.carrot.carrot_serv import nav_type_mapping
    expected = {code for code, (_, _, group) in nav_type_mapping.items() if group in {1, 2, 3, 4, 5}}
    assert BLOCK_TURN_TYPES == expected


class TestDesireHelperSeam:
  """DesireHelper 接缝：navi_lc_allowed=False 时不得发起变道（照 ATC 冻结测试）。"""

  def _pre_lane_change_dh(self):
    dh = DesireHelper()
    dh.frame = 100
    dh.lane_change_speed_floor_ms = 0.0
    dh.lane_change_state = 1  # preLaneChange
    dh.lane_change_direction = 1  # left
    cs = car.CarState.new_message(leftBlinker=True, vEgo=30.0, steeringPressed=True, steeringTorque=1.0)
    return dh, cs

  def test_gate_blocks_lane_change_start(self):
    dh, cs = self._pre_lane_change_dh()
    dh.navi_lc_allowed = False
    dh.update(cs, True, 0.0)
    assert dh.lane_change_state == 1  # 仍 preLaneChange

  def test_gate_allows_lane_change_start(self):
    dh, cs = self._pre_lane_change_dh()
    dh.navi_lc_allowed = True
    dh.update(cs, True, 0.0)
    assert dh.lane_change_state == 2  # laneChangeStarting

  def test_default_value_is_fail_open(self):
    dh, cs = self._pre_lane_change_dh()
    assert dh.navi_lc_allowed is True  # 未接闸门时行为不变
    dh.update(cs, True, 0.0)
    assert dh.lane_change_state == 2


if __name__ == "__main__":
  pytest.main([__file__])

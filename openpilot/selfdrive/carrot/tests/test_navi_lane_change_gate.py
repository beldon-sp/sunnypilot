"""导航变道许可闸门测试：fail-open 契约 + 抑制条件 + 方向冲突 + DesireHelper 接缝。

- fail-open：闸门未启用 / carrotmate 掉线 / 数据无效 → 恒放行（= 现有纯拨杆 ALC）
- 抑制：临近转向/匝道（<300m）、crossroad（<150m）、单车道，仅在数据新鲜时生效
- crossroad 兜底（#7）：crossroad 缺席（高德 SDK 路径）时以 trafficSignal 作
  路口临近代理；crossroad 在场（广播路径）时红绿灯流不参与抑制
- 方向冲突（#1）：导航转向方向与拨杆方向相反 → 阻断，默认关
  （NaviLaneChangeDirectionEnabled）
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
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper, LaneChangeDirection
from opendbc.car.structs import car


def _meta(present=True):
  return SimpleNamespace(present=present)


def _navi(connected=True, lane_count=3, lane_present=True,
          turn_type=-1, turn_distance=1000, guidance_present=True,
          crossroad_visible=False, crossroad_distance=0, crossroad_present=True,
          next_turn_type=-1, next_turn_distance=1000, next_guidance_present=True,
          signal_visible=False, signal_distance=0, signal_present=True):
  return SimpleNamespace(
    connected=connected,
    laneCurrent=SimpleNamespace(meta=_meta(lane_present), count=lane_count),
    guidanceCurrent=SimpleNamespace(meta=_meta(guidance_present), turnType=turn_type, distanceM=turn_distance),
    guidanceNext=SimpleNamespace(meta=_meta(next_guidance_present), turnType=next_turn_type, distanceM=next_turn_distance),
    crossroad=SimpleNamespace(meta=_meta(crossroad_present), visible=crossroad_visible, distanceM=crossroad_distance),
    trafficSignal=SimpleNamespace(meta=_meta(signal_present), visible=signal_visible, distanceM=signal_distance),
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


class TestCrossroadSignalFallback:
  """#7：crossroad 缺席（高德 SDK 路径）时以 trafficSignal 作路口临近代理。"""

  def _enabled_gate(self):
    gate = fresh_gate()
    gate.enabled = True
    return gate

  def test_signal_fallback_blocks(self):
    # crossroad 流缺席 + 红绿灯可见且 <150m → 抑制（SDK 路径主路径）
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(crossroad_present=False, signal_visible=True, signal_distance=149))) is False
    assert gate.block_reason == "near_crossroad"

  def test_signal_fallback_distance_boundary(self):
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(crossroad_present=False, signal_visible=True, signal_distance=150))) is True
    assert gate.update(FakeSM(_navi(crossroad_present=False, signal_visible=True, signal_distance=0))) is True

  def test_signal_invisible_or_absent_allows(self):
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(crossroad_present=False, signal_visible=False, signal_distance=50))) is True
    assert gate.update(FakeSM(_navi(crossroad_present=False, signal_present=False))) is True

  def test_crossroad_present_signal_ignored(self):
    # 广播路径：crossroad 在场但不可见时，红绿灯流不得兜底抑制
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(crossroad_present=True, crossroad_visible=False,
                                    signal_visible=True, signal_distance=50))) is True

  def test_crossroad_absent_but_signal_far_allows(self):
    gate = self._enabled_gate()
    assert gate.update(FakeSM(_navi(crossroad_present=False, signal_visible=True, signal_distance=500))) is True


class TestDirectionConflict:
  """#1：导航转向方向与拨杆方向相反 → 阻断；默认关（NaviLaneChangeDirectionEnabled）。"""

  def _navi_gate(self, direction_enabled=False):
    gate = fresh_gate()
    gate.params.put_bool("NaviGuidedLaneChangeEnabled", True, block=True)
    gate.params.put_bool("NaviLaneChangeDirectionEnabled", direction_enabled, block=True)
    gate._read_params()
    return gate

  def test_disabled_by_default(self):
    # 方向冲突条件默认关闭：即使方向相反也放行
    gate = self._navi_gate(direction_enabled=False)
    navi = _navi(turn_type=12, turn_distance=400)  # 左转 400m（超 near_turn 窗口）
    assert gate.update(FakeSM(navi), LaneChangeDirection.right) is True

  def test_opposite_direction_blocks(self):
    gate = self._navi_gate(direction_enabled=True)
    navi = _navi(turn_type=12, turn_distance=400)  # guidanceCurrent 左转 400m
    assert gate.update(FakeSM(navi), LaneChangeDirection.right) is False
    assert gate.block_reason == "direction_conflict"

  def test_same_direction_allows(self):
    gate = self._navi_gate(direction_enabled=True)
    navi = _navi(turn_type=13, turn_distance=400)  # 右转 400m
    assert gate.update(FakeSM(navi), LaneChangeDirection.right) is True

  def test_next_guidance_conflict_blocks(self):
    gate = self._navi_gate(direction_enabled=True)
    # guidanceCurrent 无转向，guidanceNext 左转 350m → 拨右杆阻断
    navi = _navi(turn_type=-1, next_turn_type=12, next_turn_distance=350)
    assert gate.update(FakeSM(navi), LaneChangeDirection.right) is False
    assert gate.block_reason == "direction_conflict"

  def test_distance_boundary_and_invalid_distance(self):
    gate = self._navi_gate(direction_enabled=True)
    assert gate.update(FakeSM(_navi(turn_type=12, turn_distance=500)), LaneChangeDirection.right) is True
    assert gate.update(FakeSM(_navi(turn_type=12, turn_distance=0)), LaneChangeDirection.right) is True

  def test_unknown_uturn_arrive_types_allow(self):
    gate = self._navi_gate(direction_enabled=True)
    assert gate.update(FakeSM(_navi(turn_type=14, turn_distance=400)), LaneChangeDirection.right) is True   # uturn → '' fail-open
    assert gate.update(FakeSM(_navi(turn_type=201, turn_distance=400)), LaneChangeDirection.right) is True  # arrive → ''
    assert gate.update(FakeSM(_navi(turn_type=999, turn_distance=400)), LaneChangeDirection.right) is True  # 未知编码

  def test_no_blinker_direction_skips_layer(self):
    gate = self._navi_gate(direction_enabled=True)
    assert gate.update(FakeSM(_navi(turn_type=12, turn_distance=400))) is True  # 未传方向：只跑其余抑制层


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

"""carrot 导航预警测试（#2 红绿灯 / #5 转向预告 / 偏航 / 连续转弯 / 即将到达）。

- fail-open：carrotNavi 掉线/无效/断连 → 无事件
- 优先级：偏航 > 红灯 > 连续转弯 > 即将到达 > 转向预告，同帧至多一条
- 红灯仅接近时（vEgo > 3 m/s）；转向预告窗口 (150, 800]m
- 连续转弯/即将到达为一次性预警，路线状态翻转（epoch 变化）后重新武装
- 连续转弯判定 fail-inert：任一 maneuver 未知编码 → 不发
- EVENTS_SP 回调可渲染出中文文案
- 回归（2026-11-22 实车崩溃）：update() 不得访问 sm['carState']——selfdrived
  的 SubMaster 未订阅 carState，访问即 KeyError（FakeSM 已模拟此行为）
"""
import time

import pytest

from openpilot.cereal import custom
from openpilot.selfdrive.carrot.navi_alerts import (
  ARRIVAL_SOON_DISTANCE_MAX,
  CARROT_NAVI_TIMEOUT,
  CONSECUTIVE_TURN_NEXT_DISTANCE_MAX,
  NaviAlertTracker,
  is_turn_family,
  turn_text,
)
from opendbc.car.structs import car


def _navi(connected=True, off_route=False,
          signal_visible=False, signal_distance=0, signal_present=True,
          red_valid=False, red_on=False,
          turn_type=-1, turn_distance=1000, guidance_present=True,
          next_turn_type=-1, next_turn_distance=1000, next_guidance_present=False,
          route_present=False, guidance_active=False, remaining_distance=0):
  navi = custom.CarrotNaviState.new_message()
  navi.connected = connected
  navi.navigationStatus.meta.present = True
  navi.navigationStatus.offRoute = off_route
  navi.navigationStatus.guidanceActive = guidance_active
  navi.trafficSignal.meta.present = signal_present
  navi.trafficSignal.visible = signal_visible
  navi.trafficSignal.distanceM = signal_distance
  navi.trafficSignal.redValid = red_valid
  navi.trafficSignal.redOn = red_on
  navi.guidanceCurrent.meta.present = guidance_present
  navi.guidanceCurrent.turnType = turn_type
  navi.guidanceCurrent.distanceM = turn_distance
  navi.guidanceNext.meta.present = next_guidance_present
  navi.guidanceNext.turnType = next_turn_type
  navi.guidanceNext.distanceM = next_turn_distance
  navi.route.meta.present = route_present
  navi.route.remainingDistanceM = remaining_distance
  return navi


class FakeSM:
  """carrotNavi 的最小替身；与真实 SubMaster 一致，访问 carState 即 KeyError。"""

  def __init__(self, navi=None, recv_age=0.0, subscribed=True, v_ego=10.0):
    now = time.monotonic()
    self.seen = {'carrotNavi': subscribed}
    self.alive = {'carrotNavi': subscribed and navi is not None}
    self.valid = {'carrotNavi': subscribed and navi is not None}
    self.recv_time = {'carrotNavi': now - recv_age}
    self._navi = navi
    self.v_ego = v_ego

  def __getitem__(self, key):
    if key == 'carrotNavi':
      return self._navi
    raise KeyError(key)  # selfdrived 的 SM 里没有 carState 等其他服务


def _update(tracker, navi=None, **kw) -> FakeSM:
  """按 selfdrived 的真实调用形态驱动 tracker（v_ego 显式传入）。"""
  sm = FakeSM(navi, **kw)
  tracker.update(sm, sm.v_ego)
  return sm


def _feed(tracker, navi, **kw):
  """多帧序列驱动：同一 tracker 逐帧喂入，返回 tracker 本身。"""
  _update(tracker, navi, **kw)
  return tracker


@pytest.fixture
def tracker():
  return NaviAlertTracker()


class TestFailOpen:
  def test_no_service_no_event(self, tracker, ):
    _update(tracker, subscribed=False)
    assert tracker.event is None

  def test_stale_no_event(self, tracker, ):
    _update(tracker, _navi(), recv_age=CARROT_NAVI_TIMEOUT + 0.1)
    assert tracker.event is None

  def test_disconnected_no_event(self, tracker, ):
    # carrotmate 断连但 publisher 心跳仍在发
    _update(tracker, _navi(connected=False))
    assert tracker.event is None

  def test_clean_state_no_event(self, tracker, ):
    _update(tracker, _navi())
    assert tracker.event is None


class TestRegressionCarState:
  """2026-11-22 实车 selfdrived 崩溃循环回归：禁止访问 sm['carState']。"""

  def test_update_never_touches_car_state(self, tracker):
    # FakeSM.__getitem__ 对 carState 直接 KeyError；若实现回归，此用例必炸
    _update(tracker, _navi(signal_visible=True, signal_distance=100,
                           red_valid=True, red_on=True))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotTrafficSignal

  def test_car_state_access_raises(self, tracker):
    # 防御性：确认替身真的会抛（替身语义有效性自检）
    sm = FakeSM(_navi())
    with pytest.raises(KeyError):
      sm['carState']


class TestPriorities:
  def test_off_route_wins(self, tracker, ):
    navi = _navi(off_route=True, signal_visible=True, signal_distance=100,
                 red_valid=True, red_on=True, turn_type=12, turn_distance=400)
    _update(tracker, navi)
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavOffRoute

  def test_red_light_wins_over_turn(self, tracker, ):
    navi = _navi(signal_visible=True, signal_distance=200, red_valid=True, red_on=True,
                 turn_type=12, turn_distance=400)
    _update(tracker, navi)
    assert tracker.event == custom.OnroadEventSP.EventName.carrotTrafficSignal


class TestTrafficSignalAlert:
  def test_red_light_near_alerts(self, tracker, ):
    navi = _navi(signal_visible=True, signal_distance=299, red_valid=True, red_on=True)
    _update(tracker, navi)
    assert tracker.event == custom.OnroadEventSP.EventName.carrotTrafficSignal

  def test_distance_boundary(self, tracker, ):
    _update(tracker, _navi(signal_visible=True, signal_distance=300, red_valid=True, red_on=True))
    assert tracker.event is None
    _update(tracker, _navi(signal_visible=True, signal_distance=301, red_valid=True, red_on=True))
    assert tracker.event is None

  def test_green_light_no_alert(self, tracker, ):
    navi = _navi(signal_visible=True, signal_distance=100, red_valid=True, red_on=False)
    _update(tracker, navi)
    assert tracker.event is None

  def test_stationary_no_alert(self, tracker, ):
    # 等灯时（vEgo <= 3）不打扰
    navi = _navi(signal_visible=True, signal_distance=50, red_valid=True, red_on=True)
    _update(tracker, navi, v_ego=2.0)
    assert tracker.event is None


class TestNavTurnAlert:
  def test_turn_in_window_alerts(self, tracker, ):
    navi = _navi(turn_type=12, turn_distance=400)  # 左转 400m
    _update(tracker, navi)
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavTurn

  def test_window_boundaries(self, tracker, ):
    _update(tracker, _navi(turn_type=12, turn_distance=150))
    assert tracker.event is None  # <150m 属 ATC/闸门管辖
    _update(tracker, _navi(turn_type=12, turn_distance=800))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavTurn
    _update(tracker, _navi(turn_type=12, turn_distance=801))
    assert tracker.event is None

  def test_unknown_turn_type_no_alert(self, tracker, ):
    _update(tracker, _navi(turn_type=999, turn_distance=400))
    assert tracker.event is None

  def test_notification_type_no_alert(self, tracker, ):
    _update(tracker, _navi(turn_type=51, turn_distance=400))
    assert tracker.event is None


class TestTurnFamily:
  """is_turn_family：连续转弯预警的判定口径（fail-inert）。"""

  def test_turn_and_rotary_in_family(self):
    assert is_turn_family(12)   # turn left
    assert is_turn_family(13)   # turn right
    assert is_turn_family(14)   # uturn（turn 家族）
    assert is_turn_family(131)  # rotary

  def test_non_turn_and_unknown_excluded(self):
    assert not is_turn_family(7)    # fork
    assert not is_turn_family(1006)  # off ramp
    assert not is_turn_family(201)  # arrive
    assert not is_turn_family(51)   # notification
    assert not is_turn_family(999)  # 未知编码 fail-inert
    assert not is_turn_family(-1)   # 无转向


class TestConsecutiveTurnsAlert:
  """连续转弯（一次性）：当前+下一 maneuver 均 turn/rotary，next ∈ (0, 400]m。"""

  def _consecutive_navi(self, **kw):
    base = dict(turn_type=12, turn_distance=400,  # 当前左转 400m
                next_turn_type=13, next_turn_distance=300, next_guidance_present=True)
    base.update(kw)
    return _navi(**base)

  def test_consecutive_turns_alerts(self, tracker):
    _update(tracker, self._consecutive_navi())
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavConsecutiveTurns

  def test_one_shot_no_repeat(self, tracker):
    navi = self._consecutive_navi()
    _feed(tracker, navi)
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavConsecutiveTurns
    # 同条件第二帧：不再响（一次性）；回落到连续的转向预告
    _feed(tracker, navi)
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavTurn

  def test_next_distance_boundary(self, tracker):
    _update(tracker, self._consecutive_navi(next_turn_distance=CONSECUTIVE_TURN_NEXT_DISTANCE_MAX))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavConsecutiveTurns
    tracker2 = NaviAlertTracker()
    _update(tracker2, self._consecutive_navi(next_turn_distance=CONSECUTIVE_TURN_NEXT_DISTANCE_MAX + 1))
    assert tracker2.event == custom.OnroadEventSP.EventName.carrotNavTurn  # 窗外不触发

  def test_next_distance_zero_no_alert(self, tracker):
    # next 0m 视为无效数据：不触发连续转弯，但当前转向预告照常（连续预警）
    _update(tracker, self._consecutive_navi(next_turn_distance=0))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavTurn

  def test_unknown_next_turn_fail_inert(self, tracker):
    # 未知 turnCode 永远不发，绝不误发
    _update(tracker, self._consecutive_navi(next_turn_type=999))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavTurn

  def test_unknown_current_turn_fail_inert(self, tracker):
    # 当前 maneuver 未知编码：连续转弯不发；转向预告也因文案映射缺失而不发
    _update(tracker, self._consecutive_navi(turn_type=999))
    assert tracker.event is None

  def test_fork_off_ramp_not_turn_family(self, tracker):
    _update(tracker, self._consecutive_navi(next_turn_type=7))  # fork
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavTurn
    tracker2 = NaviAlertTracker()
    _update(tracker2, self._consecutive_navi(next_turn_type=1006))  # off ramp
    assert tracker2.event == custom.OnroadEventSP.EventName.carrotNavTurn

  def test_rotary_counts_as_turn_family(self, tracker):
    _update(tracker, self._consecutive_navi(next_turn_type=131))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavConsecutiveTurns

  def test_next_absent_no_alert(self, tracker):
    _update(tracker, self._consecutive_navi(next_guidance_present=False))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavTurn


class TestArrivalSoonAlert:
  """即将到达（一次性）：route.present + guidanceActive + 剩余里程首次 ≤500m。"""

  def _arrival_navi(self, **kw):
    base = dict(route_present=True, guidance_active=True, remaining_distance=500)
    base.update(kw)
    return _navi(**base)

  def test_arrival_alerts(self, tracker):
    _update(tracker, self._arrival_navi())
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavArrivalSoon

  def test_one_shot_no_repeat(self, tracker):
    navi = self._arrival_navi()
    _feed(tracker, navi)
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavArrivalSoon
    _feed(tracker, navi)
    assert tracker.event is None  # 一次性：恢复后不再响

  def test_distance_boundary(self, tracker):
    _update(tracker, self._arrival_navi(remaining_distance=ARRIVAL_SOON_DISTANCE_MAX))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavArrivalSoon
    tracker2 = NaviAlertTracker()
    _update(tracker2, self._arrival_navi(remaining_distance=ARRIVAL_SOON_DISTANCE_MAX + 1))
    assert tracker2.event is None

  def test_zero_remaining_no_alert(self, tracker):
    # 0m 视为已到达/无效数据，不发
    _update(tracker, self._arrival_navi(remaining_distance=0))
    assert tracker.event is None

  def test_no_guidance_no_alert(self, tracker):
    _update(tracker, self._arrival_navi(guidance_active=False))
    assert tracker.event is None

  def test_route_absent_no_alert(self, tracker):
    _update(tracker, self._arrival_navi(route_present=False))
    assert tracker.event is None


class TestRouteEpochRearm:
  """路线状态 epoch 翻转 → 一次性预警重新武装（tc-v61 P3 re-arm 模式）。

  本地 generation 是全量状态计数器（每帧更新都变），不能当"重算路"信号；
  re-arm 触发用 route.present / routePresent / offRoute 三元组边沿。
  """

  def test_rearm_after_off_route_cycle(self, tracker):
    navi = dict(turn_type=12, turn_distance=400,
                next_turn_type=13, next_turn_distance=300, next_guidance_present=True)
    _feed(tracker, _navi(**navi))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavConsecutiveTurns
    _feed(tracker, _navi(**navi))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavTurn  # 已消化

    # 偏航（epoch 翻转）
    _feed(tracker, _navi(**navi, off_route=True))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavOffRoute
    # 重算路完成回到线上（epoch 再翻转）→ 重新武装 → 再次响起
    _feed(tracker, _navi(**navi))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavConsecutiveTurns

  def test_rearm_after_route_reappears(self, tracker):
    base = dict(route_present=True, guidance_active=True, remaining_distance=400)
    _feed(tracker, _navi(**base))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavArrivalSoon
    _feed(tracker, _navi(**base))
    assert tracker.event is None

    # 路线消失（epoch 翻转）→ 无事件但已 re-arm
    _feed(tracker, _navi(route_present=False, guidance_active=True, remaining_distance=400))
    assert tracker.event is None
    # 新路线出现 → 再次响一次
    _feed(tracker, _navi(**base))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavArrivalSoon

  def test_off_route_rising_edge_also_rearms(self, tracker):
    navi = dict(route_present=True, guidance_active=True, remaining_distance=400)
    _feed(tracker, _navi(**navi))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavArrivalSoon
    # 偏航边沿：偏航预警（连续）在 offRoute 期间每帧优先展示，
    # 但到达预警已重新武装；回到线上后再次响一次
    _feed(tracker, _navi(**navi, off_route=True))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavOffRoute
    _feed(tracker, _navi(**navi, off_route=True))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavOffRoute
    _feed(tracker, _navi(**navi))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavArrivalSoon

  def test_first_frame_is_baseline_no_spurious_rearm(self, tracker):
    # 首帧建基线：条件成立即响（armed 初始 True），但 epoch 不触发额外行为
    _update(tracker, _navi(turn_type=12, turn_distance=400,
                           next_turn_type=13, next_turn_distance=300,
                           next_guidance_present=True))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavConsecutiveTurns


class TestNewAdvisoryPriority:
  """新预警插入优先级链：偏航 > 红灯 > 连续转弯 > 即将到达 > 转向预告。"""

  def test_consecutive_beats_arrival(self, tracker):
    navi = _navi(turn_type=12, turn_distance=400,
                 next_turn_type=13, next_turn_distance=300, next_guidance_present=True,
                 route_present=True, guidance_active=True, remaining_distance=300)
    _update(tracker, navi)
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavConsecutiveTurns

  def test_arrival_beats_turn_preview(self, tracker):
    _update(tracker, _navi(turn_type=12, turn_distance=400,
                           route_present=True, guidance_active=True, remaining_distance=300))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavArrivalSoon

  def test_off_route_still_wins(self, tracker):
    navi = _navi(off_route=True, turn_type=12, turn_distance=400,
                 next_turn_type=13, next_turn_distance=300, next_guidance_present=True,
                 route_present=True, guidance_active=True, remaining_distance=300)
    _update(tracker, navi)
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavOffRoute

  def test_red_light_still_wins(self, tracker):
    navi = _navi(signal_visible=True, signal_distance=100, red_valid=True, red_on=True,
                 turn_type=12, turn_distance=400,
                 next_turn_type=13, next_turn_distance=300, next_guidance_present=True)
    _update(tracker, navi)
    assert tracker.event == custom.OnroadEventSP.EventName.carrotTrafficSignal


class TestTurnText:
  def test_known_types(self):
    assert turn_text(12) == "左转"
    assert turn_text(13) == "右转"
    assert turn_text(1006) == "驶出匝道"
    assert turn_text(201) == "到达目的地"
    assert turn_text(14) == "掉头"

  def test_unknown_types(self):
    assert turn_text(999) == ""
    assert turn_text(-1) == ""
    assert turn_text(51) == ""  # notification


class TestAlertRendering:
  """EVENTS_SP 回调渲染：文案从 sm['carrotNavi'] 现算。"""

  def test_callbacks_render(self, ):
    from openpilot.sunnypilot.selfdrive.selfdrived.events import (
      carrot_traffic_signal_alert,
      carrot_nav_turn_alert,
      carrot_nav_off_route_alert,
    )
    CP = car.CarParams.new_message()
    CS = car.CarState.new_message()
    navi = _navi(signal_visible=True, signal_distance=250, red_valid=True, red_on=True,
                 turn_type=12, turn_distance=400)
    sm = FakeSM(navi)

    a1 = carrot_traffic_signal_alert(CP, CS, sm, True, 0, None)
    assert "250" in a1.alert_text_1
    a2 = carrot_nav_turn_alert(CP, CS, sm, True, 0, None)
    assert "400" in a2.alert_text_1 and "左转" in a2.alert_text_1
    from openpilot.cereal import log
    a3 = carrot_nav_off_route_alert(CP, CS, sm, True, 0, None)
    assert a3.alert_status == log.SelfdriveState.AlertStatus.userPrompt

  def test_new_advisory_callbacks_render(self):
    from openpilot.sunnypilot.selfdrive.selfdrived.events import (
      carrot_nav_arrival_soon_alert,
      carrot_nav_consecutive_turns_alert,
    )
    CP = car.CarParams.new_message()
    CS = car.CarState.new_message()
    navi = _navi(turn_type=12, turn_distance=400,
                 next_turn_type=13, next_turn_distance=300, next_guidance_present=True)
    sm = FakeSM(navi)
    a1 = carrot_nav_consecutive_turns_alert(CP, CS, sm, True, 0, None)
    assert "400" in a1.alert_text_1 and "300" in a1.alert_text_1
    assert "左转" in a1.alert_text_1 and "右转" in a1.alert_text_1

    navi2 = _navi(route_present=True, guidance_active=True, remaining_distance=480)
    sm2 = FakeSM(navi2)
    a2 = carrot_nav_arrival_soon_alert(CP, CS, sm2, True, 0, None)
    assert "480" in a2.alert_text_1


if __name__ == "__main__":
  pytest.main([__file__])

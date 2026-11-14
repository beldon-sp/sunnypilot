"""carrot 导航预警测试（#2 红绿灯 / #5 转向预告 / 偏航）。

- fail-open：carrotNavi 掉线/无效/断连 → 无事件
- 优先级：偏航 > 红灯 > 转向预告，同帧至多一条
- 红灯仅接近时（vEgo > 3 m/s）；转向预告窗口 (150, 800]m
- EVENTS_SP 回调可渲染出中文文案
"""
import time
from types import SimpleNamespace

import pytest

from openpilot.cereal import custom
from openpilot.selfdrive.carrot.navi_alerts import (
  CARROT_NAVI_TIMEOUT,
  NaviAlertTracker,
  turn_text,
)
from opendbc.car.structs import car


def _navi(connected=True, off_route=False,
          signal_visible=False, signal_distance=0, signal_present=True,
          red_valid=False, red_on=False,
          turn_type=-1, turn_distance=1000, guidance_present=True):
  navi = custom.CarrotNaviState.new_message()
  navi.connected = connected
  navi.navigationStatus.meta.present = True
  navi.navigationStatus.offRoute = off_route
  navi.trafficSignal.meta.present = signal_present
  navi.trafficSignal.visible = signal_visible
  navi.trafficSignal.distanceM = signal_distance
  navi.trafficSignal.redValid = red_valid
  navi.trafficSignal.redOn = red_on
  navi.guidanceCurrent.meta.present = guidance_present
  navi.guidanceCurrent.turnType = turn_type
  navi.guidanceCurrent.distanceM = turn_distance
  return navi


class FakeSM:
  """carrotNavi + carState 的最小替身，None = 没数据。"""

  def __init__(self, navi=None, recv_age=0.0, subscribed=True, v_ego=10.0):
    now = time.monotonic()
    self.seen = {'carrotNavi': subscribed}
    self.alive = {'carrotNavi': subscribed and navi is not None}
    self.valid = {'carrotNavi': subscribed and navi is not None}
    self.recv_time = {'carrotNavi': now - recv_age}
    self._navi = navi
    self._car_state = SimpleNamespace(vEgo=v_ego)

  def __getitem__(self, key):
    return self._navi if key == 'carrotNavi' else self._car_state


@pytest.fixture
def tracker():
  return NaviAlertTracker()


class TestFailOpen:
  def test_no_service_no_event(self, tracker, ):
    tracker.update(FakeSM(subscribed=False))
    assert tracker.event is None

  def test_stale_no_event(self, tracker, ):
    tracker.update(FakeSM(_navi(), recv_age=CARROT_NAVI_TIMEOUT + 0.1))
    assert tracker.event is None

  def test_disconnected_no_event(self, tracker, ):
    # carrotmate 断连但 publisher 心跳仍在发
    tracker.update(FakeSM(_navi(connected=False)))
    assert tracker.event is None

  def test_clean_state_no_event(self, tracker, ):
    tracker.update(FakeSM(_navi()))
    assert tracker.event is None


class TestPriorities:
  def test_off_route_wins(self, tracker, ):
    navi = _navi(off_route=True, signal_visible=True, signal_distance=100,
                 red_valid=True, red_on=True, turn_type=12, turn_distance=400)
    tracker.update(FakeSM(navi))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavOffRoute

  def test_red_light_wins_over_turn(self, tracker, ):
    navi = _navi(signal_visible=True, signal_distance=200, red_valid=True, red_on=True,
                 turn_type=12, turn_distance=400)
    tracker.update(FakeSM(navi))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotTrafficSignal


class TestTrafficSignalAlert:
  def test_red_light_near_alerts(self, tracker, ):
    navi = _navi(signal_visible=True, signal_distance=299, red_valid=True, red_on=True)
    tracker.update(FakeSM(navi))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotTrafficSignal

  def test_distance_boundary(self, tracker, ):
    tracker.update(FakeSM(_navi(signal_visible=True, signal_distance=300, red_valid=True, red_on=True)))
    assert tracker.event is None
    tracker.update(FakeSM(_navi(signal_visible=True, signal_distance=301, red_valid=True, red_on=True)))
    assert tracker.event is None

  def test_green_light_no_alert(self, tracker, ):
    navi = _navi(signal_visible=True, signal_distance=100, red_valid=True, red_on=False)
    tracker.update(FakeSM(navi))
    assert tracker.event is None

  def test_stationary_no_alert(self, tracker, ):
    # 等灯时（vEgo <= 3）不打扰
    navi = _navi(signal_visible=True, signal_distance=50, red_valid=True, red_on=True)
    tracker.update(FakeSM(navi, v_ego=2.0))
    assert tracker.event is None


class TestNavTurnAlert:
  def test_turn_in_window_alerts(self, tracker, ):
    navi = _navi(turn_type=12, turn_distance=400)  # 左转 400m
    tracker.update(FakeSM(navi))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavTurn

  def test_window_boundaries(self, tracker, ):
    tracker.update(FakeSM(_navi(turn_type=12, turn_distance=150)))
    assert tracker.event is None  # <150m 属 ATC/闸门管辖
    tracker.update(FakeSM(_navi(turn_type=12, turn_distance=800)))
    assert tracker.event == custom.OnroadEventSP.EventName.carrotNavTurn
    tracker.update(FakeSM(_navi(turn_type=12, turn_distance=801)))
    assert tracker.event is None

  def test_unknown_turn_type_no_alert(self, tracker, ):
    tracker.update(FakeSM(_navi(turn_type=999, turn_distance=400)))
    assert tracker.event is None

  def test_notification_type_no_alert(self, tracker, ):
    tracker.update(FakeSM(_navi(turn_type=51, turn_distance=400)))
    assert tracker.event is None


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


if __name__ == "__main__":
  pytest.main([__file__])

"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import math
import platform
from types import SimpleNamespace
from unittest.mock import MagicMock

from openpilot.cereal import custom
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET
from openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control.map_controller import R, SmartCruiseControlMap
from openpilot.common.test import OpenpilotTestCase

MapState = VisionState = custom.LongitudinalPlanSP.SmartCruiseControl.MapState


class TestSmartCruiseControlMap(OpenpilotTestCase):

  def setup_method(self):
    self.params = Params()
    self.mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else self.params
    self.reset_params()
    self.scc_m = SmartCruiseControlMap()
    self.sm = MagicMock()
    self.sm.__getitem__.return_value = SimpleNamespace(latitude=0.0, longitude=0.0)

  def reset_params(self):
    self.params.put_bool("SmartCruiseControlMap", True, block=True)

    self.params.put("MapTargetVelocities", "{}", block=True)

  def test_initial_state(self):
    assert self.scc_m.state == VisionState.disabled
    assert not self.scc_m.is_active
    assert self.scc_m.output_v_target == V_CRUISE_UNSET
    assert self.scc_m.output_a_target == 0.

  def test_system_disabled(self):
    self.params.put_bool("SmartCruiseControlMap", False, block=True)
    self.scc_m.enabled = self.params.get_bool("SmartCruiseControlMap")

    for _ in range(int(10. / DT_MDL)):
      self.scc_m.update(self.sm, True, False, 0., 0., 0.)
    assert self.scc_m.state == VisionState.disabled
    assert not self.scc_m.is_active

  def test_disabled(self):
    for _ in range(int(10. / DT_MDL)):
      self.scc_m.update(self.sm, False, False, 0., 0., 0.)
    assert self.scc_m.state == VisionState.disabled

  def test_transition_disabled_to_enabled(self):
    for _ in range(int(10. / DT_MDL)):
      self.scc_m.update(self.sm, True, False, 0., 0., 0.)
    assert self.scc_m.state == VisionState.enabled

  def test_moderate_curve(self):
    # Regression: `... / 2 * a` parsed as `(.../2)*a` instead of `.../(2*a)`,
    # making max_d ~11x too small so the moderate-curve branch never tripped.
    # v_ego=25, a_ego=0, tv=24: fixed max_d≈45m vs buggy ≈4m at a 40m waypoint.
    waypoint_lon_deg = (40.0 / R) * (180.0 / math.pi)
    self.mem_params.put("MapTargetVelocities",
                        json.dumps([{"latitude": 0.0, "longitude": waypoint_lon_deg, "velocity": 24.0}]), block=True)

    self.scc_m.update(self.sm, True, False, 25.0, 0.0, 30.0)

    self.assertAlmostEqual(self.scc_m.v_target, 24.0, delta=24.0 * 1e-6)

  # TODO-SP: mock data from modelV2 to test other states

  # ── carrot polyline 曲率预览（#4）──

  def _carrot_sm(self, polyline, sequence=1, fresh=True, connected=True, enabled=True):
    """carrotNavi + gpsLocation 的最小替身。"""
    navi = SimpleNamespace(
      connected=connected,
      route=SimpleNamespace(
        meta=SimpleNamespace(present=True),
        sequence=sequence,
        polyline=[SimpleNamespace(latitude=la, longitude=lo) for la, lo in polyline],
      ),
    )
    sm = MagicMock()
    sm.seen = {'carrotNavi': True}
    sm.alive = {'carrotNavi': fresh}
    sm.valid = {'carrotNavi': fresh}
    sm.recv_time = {'carrotNavi': __import__('time').monotonic() - (0.0 if fresh else 10.0)}
    sm.__getitem__.side_effect = lambda key: navi if key == 'carrotNavi' else SimpleNamespace(latitude=0.0, longitude=0.0)
    return sm

  def test_carrot_preview_used_when_enabled(self):
    # 开关开 + carrotNavi 新鲜 → 用 polyline 曲率候选（含直角弯 → 出现低速点）
    self.params.put_bool("CarrotRouteCurvatureEnabled", True, block=True)
    self.scc_m.carrot_enabled = True
    corner = ([(i * 0.0009, 0.0) for i in range(5)] +
              [(0.0036, i * 0.0009) for i in range(1, 5)])
    self.scc_m._update_target_velocities(self._carrot_sm(corner))
    assert self.scc_m.target_velocities
    assert any(p['velocity'] < 30.0 for p in self.scc_m.target_velocities)

  def test_carrot_preview_fallback_when_disabled(self):
    # 开关关（默认）→ 走 OSM MapTargetVelocities param
    self.params.put_bool("CarrotRouteCurvatureEnabled", False, block=True)
    self.scc_m.carrot_enabled = False
    corner = ([(i * 0.0009, 0.0) for i in range(5)] +
              [(0.0036, i * 0.0009) for i in range(1, 5)])
    self.scc_m._update_target_velocities(self._carrot_sm(corner))
    assert self.scc_m.target_velocities == []  # param 为空 {} → []

  def test_carrot_preview_fallback_when_stale(self):
    # carrotNavi 1.5s 无新帧（掉线）→ 回落 OSM param（fail-open）
    self.params.put_bool("CarrotRouteCurvatureEnabled", True, block=True)
    self.scc_m.carrot_enabled = True
    corner = ([(i * 0.0009, 0.0) for i in range(5)] +
              [(0.0036, i * 0.0009) for i in range(1, 5)])
    self.scc_m._update_target_velocities(self._carrot_sm(corner, fresh=False))
    assert self.scc_m.target_velocities == []
    assert self.scc_m.carrot_preview.valid is False

  def test_carrot_preview_fallback_when_disconnected(self):
    self.params.put_bool("CarrotRouteCurvatureEnabled", True, block=True)
    self.scc_m.carrot_enabled = True
    corner = ([(i * 0.0009, 0.0) for i in range(5)] +
              [(0.0036, i * 0.0009) for i in range(1, 5)])
    self.scc_m._update_target_velocities(self._carrot_sm(corner, connected=False))
    assert self.scc_m.target_velocities == []

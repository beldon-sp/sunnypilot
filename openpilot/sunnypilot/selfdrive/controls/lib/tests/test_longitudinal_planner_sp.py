"""LongitudinalPlannerSP 源仲裁滞回回归（2026-11-22 复盘）。

min() 仲裁在两源 vTarget 贴近时会 20Hz 乒乓（源翻转 → HUD 抖动 + 源内部
状态被来回重置）。滞回语义：留守当前源最多比绝对最小目标高
SOURCE_HYSTERESIS_MS；一旦某源明显更低则切换；安全语义（取更保守目标）不变。
"""
import pytest

from opendbc.car.structs import car
from openpilot.cereal import custom
from openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_planner import (
  LongitudinalPlannerSP, LongitudinalPlanSource, SOURCE_HYSTERESIS_MS,
)


class FakeSM:
  def __init__(self):
    self.carState = car.CarState.new_message()
    self.carState.vCruiseCluster = 80.0
    self.carControl = car.CarControl.new_message()
    self.seen = {"carrotMan": False}
    self.alive = {"carrotMan": False}
    self.valid = {"carrotMan": False}
    self.recv_time = {"carrotMan": 0.0}

  def __getitem__(self, key):
    return getattr(self, key)


class StubSource:
  def __init__(self, v, a=0.0):
    self.output_v_target = v
    self.output_a_target = a
    self.updates = 0

  def update(self, *args, **kwargs):
    self.updates += 1


@pytest.fixture
def sp():
  planner = LongitudinalPlannerSP(car.CarParams.new_message(), custom.CarParamsSP.new_message(), mpc=None)
  planner.scc = StubSource(0.0)
  planner.sla = StubSource(0.0)
  # resolver 为只读属性对象，整体替换为最小替身
  planner.resolver = StubSource(0.0)
  planner.resolver.speed_limit_valid = False
  planner.resolver.speed_limit_last_valid = False
  planner.resolver.speed_limit = 0.0
  planner.resolver.speed_limit_final_last = 0.0
  planner.resolver.distance = 0.0
  return planner


def update(sp, v_cruise, scc_v=None, sla_v=None):
  sp.scc.vision = StubSource(v_cruise if scc_v is None else scc_v)
  sp.scc.map = StubSource(250.0)
  sp.sla.output_v_target = v_cruise if sla_v is None else sla_v
  sm = FakeSM()
  return sp.update_targets(sm, v_ego=10.0, a_ego=0.0, v_cruise=v_cruise)


def test_switches_when_clearly_lower(sp):
  update(sp, v_cruise=25.0)               # 初始 cruise
  assert sp.source == LongitudinalPlanSource.cruise
  update(sp, v_cruise=25.0, scc_v=10.0)   # scc 明显更低 → 切换
  assert sp.source == LongitudinalPlanSource.sccVision
  assert sp.output_v_target == 10.0


def test_no_flap_on_near_tie(sp):
  update(sp, v_cruise=25.0, scc_v=25.0 - SOURCE_HYSTERESIS_MS / 2)
  assert sp.source == LongitudinalPlanSource.cruise  # 带内留守
  update(sp, v_cruise=25.0, scc_v=25.0 - SOURCE_HYSTERESIS_MS * 3)
  assert sp.source == LongitudinalPlanSource.sccVision  # 明显更低才切换


def test_exact_tie_keeps_source(sp):
  update(sp, v_cruise=25.0, scc_v=24.0)
  assert sp.source == LongitudinalPlanSource.sccVision
  # scc 回升到与 cruise 完全相等：不切回 cruise（避免抖振），直至 cruise 明显更低
  update(sp, v_cruise=25.0, scc_v=25.0)
  assert sp.source == LongitudinalPlanSource.sccVision
  update(sp, v_cruise=20.0, scc_v=25.0)
  assert sp.source == LongitudinalPlanSource.cruise


def test_holds_lower_target_when_switching_back(sp):
  update(sp, v_cruise=25.0, scc_v=20.0)
  assert sp.source == LongitudinalPlanSource.sccVision
  # scc 回升到带内：留守 sccVision，发布的是当前源目标（非绝对最小值，差 ≤ 滞回带）
  update(sp, v_cruise=25.0, scc_v=24.95)
  assert sp.source == LongitudinalPlanSource.sccVision
  assert sp.output_v_target == 24.95

"""radard NaN 前车回归测试（2026-11-22 实车纵向失效复盘）。

BYD 雷达接口的 slot-table 显示点按设计带 NaN vRel；NaN 一旦进入 Track 的
KF1D 便永不自愈，经 radarState.leadOne 传入 longitudinal MPC 后，设备上的
acados 求解器被永久毒化（reset() 无法恢复），纵向控制整段失效。

回归契约：
1. 非有限量测不得污染 Track 的 KF（vLeadK/aLeadK 必须保持有限）
2. 已污染的 KF 在恢复有限量测后必须自动重建
3. 任何含非有限量的 lead 不得通过 get_lead 发布（最后一道门）
"""
import math

import numpy as np

from openpilot.cereal import messaging, custom
from opendbc.car.structs import car
from openpilot.common.simple_kalman import KF1D
from openpilot.selfdrive.controls.radard import DT_MDL, SPEED, ACCEL, Track, get_lead, KalmanParams

KP = KalmanParams(DT_MDL)


def make_lead_msg(x=40.0, y=0.0, v=20.0):
  from openpilot.cereal import log
  msg = messaging.new_message('modelV2', valid=True)
  msg.modelV2.leadsV3 = [log.ModelDataV2.LeadDataV3.new_message()]
  lead = msg.modelV2.leadsV3[0]
  lead.x = [x, 0.5, 0.0]
  lead.xStd = [0.5, 0.1, 0.0]
  lead.y = [y, 0.3, 0.0]
  lead.yStd = [0.3, 0.1, 0.0]
  lead.v = [v, 0.5, 0.0]
  lead.vStd = [0.5, 0.1, 0.0]
  lead.a = [0.0, 0.3, 0.0]
  lead.aStd = [0.3, 0.1, 0.0]
  lead.prob = 0.9
  return msg.modelV2.leadsV3[0]


def cp():
  return car.CarParams.new_message(), custom.CarParamsSP.new_message()


class TestTrackNanGuard:
  def test_nan_point_keeps_kf_finite(self):
    t = Track(1, 20.0, KP)
    t.update(30.0, 0.0, 5.0, 20.0)          # 有限帧
    t.update(28.0, 0.0, float('nan'), float('nan'))  # NaN 量测
    assert math.isfinite(t.vLeadK)
    assert math.isfinite(t.aLeadK)

  def test_nan_birth_track(self):
    # 首帧即 NaN（slot-table 显示点出生即带 NaN vRel 的场景）
    t = Track(2, float('nan'), KP)
    t.update(30.0, 0.0, float('nan'), float('nan'))
    assert math.isfinite(t.vLeadK)
    assert math.isfinite(t.aLeadK)
    t.update(30.0, 0.0, 5.0, 20.0)
    assert math.isfinite(t.vLeadK)
    assert math.isfinite(t.aLeadK)
    assert abs(t.vLeadK - 20.0) < 1.0

  def test_poisoned_kf_recovers_on_finite_measurement(self):
    t = Track(3, 20.0, KP)
    # 模拟历史污染：直接把 KF 状态置 NaN
    t.kf.x[SPEED][0] = float('nan')
    t.kf.x[ACCEL][0] = float('nan')
    t.update(29.0, 0.0, 4.0, 19.0)
    assert math.isfinite(t.vLeadK)
    assert math.isfinite(t.aLeadK)
    assert abs(t.vLeadK - 19.0) < 1.0

  def test_kf_still_filters_after_guard(self):
    # NaN 插入前后，正常量测仍应被滤波（不被重建破坏）
    t = Track(4, 20.0, KP)
    t.update(30.0, 0.0, 5.0, 20.0)
    t.update(29.0, 0.0, 5.0, 20.0)
    v_before = t.vLeadK
    t.update(28.0, 0.0, float('nan'), float('nan'))
    t.update(27.0, 0.0, 5.0, 20.0)
    assert math.isfinite(t.vLeadK)
    # KF 重建后应快速收敛回量测附近
    assert abs(t.vLeadK - 20.0) < abs(v_before - 20.0) + 1.0


class TestGetLeadFiniteGate:
  def _tracks(self, **overrides):
    t = Track(10, 20.0, KP)
    t.update(30.0, 0.0, 5.0, 20.0)
    for k, v in overrides.items():
      setattr(t, k, v)
    return {10: t}

  def test_finite_lead_passes(self):
    CP, CP_SP = cp()
    lead = get_lead(15.0, True, self._tracks(), make_lead_msg(), 15.0, 0.9, CP, CP_SP)
    assert lead['present']
    assert math.isfinite(lead['dRel'])

  def test_nan_aleadk_lead_dropped(self):
    CP, CP_SP = cp()
    lead = get_lead(15.0, True, self._tracks(aLeadK=float('nan')), make_lead_msg(), 15.0, 0.9, CP, CP_SP)
    assert not lead['present']

  def test_nan_vrel_track_falls_back_to_vision(self):
    # NaN vRel 轨道被 sanity 检查拒绝后，应回退纯视觉 lead（有限值）而非发 NaN
    CP, CP_SP = cp()
    lead = get_lead(15.0, True, self._tracks(vRel=float('nan'), vLead=float('nan'), vLeadK=float('nan')),
                    make_lead_msg(), 15.0, 0.9, CP, CP_SP)
    assert lead['present']
    assert all(math.isfinite(lead[k]) for k in ('dRel', 'yRel', 'vRel', 'vLead', 'vLeadK', 'aLeadK'))

  def test_nan_drel_track_falls_back_to_vision(self):
    CP, CP_SP = cp()
    lead = get_lead(15.0, True, self._tracks(dRel=float('nan')), make_lead_msg(), 15.0, 0.9, CP, CP_SP)
    assert lead['present']
    assert all(math.isfinite(lead[k]) for k in ('dRel', 'yRel', 'vRel', 'vLead', 'vLeadK', 'aLeadK'))

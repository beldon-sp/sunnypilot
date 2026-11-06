"""carrot 巡航接缝（design D2）：纵向 cap、accel 整形、ATC 变道冻结、SLA/SCC 门。

plannerd/controlsd 各持一个实例，各自从自己的 SubMaster 视图喂 carrotMan：
- capping（guard∧mode）：min-cap 纵向目标；guard 一断（≤1s）立即放开，
  保证"数据停发 1s 回落原生链路"的硬约束。
- mode_active（滞回输出，含退出宽限 M 秒）：SLA/SCC 门与 hud setSpeed 跟随，
  搭子开关抖振期间不翻转。
- atc_active（activeCarrot>1 ∧ 0<xDistToTurn<100）：ATC 转弯减速区间，
  横向侧据此冻结变道。
"""
import numpy as np

from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.raw_params import get_int_param
from openpilot.selfdrive.carrot.carrot_man_input import get_carrot_man
from openpilot.selfdrive.carrot.carrot_mode import CarrotMode, carrot_mode_seconds_from_params

# carrot get_carrot_accel 包络（carrot_functions.py A_CRUISE_MAX_BP_CARROT / cruiseMaxVals）
A_CRUISE_MAX_BP_CARROT = [0., 10 * CV.KPH_TO_MS, 40 * CV.KPH_TO_MS, 60 * CV.KPH_TO_MS,
                          80 * CV.KPH_TO_MS, 110 * CV.KPH_TO_MS, 140 * CV.KPH_TO_MS]
CRUISE_MAX_VALS_CARROT = [1.6, 1.6, 1.2, 1.0, 0.8, 0.7, 0.6]


class CarrotCruise:
  def __init__(self, params: Params | None = None):
    self.params = params if params is not None else Params()
    self.mode = CarrotMode(*carrot_mode_seconds_from_params(self.params))
    self.mode_active = False
    self.capping = False
    self.desired_speed_kph = 0.0
    self.desired_source = ""
    self.n_road_limit_speed = 0
    self.traffic_state = 0
    self.atc_active = False
    self.active_carrot = 0
    self.x_dist_to_turn = 0
    # 巡航预设跟随道路限速（car/cruise.py presets，与 carrot 行为一致）
    self.auto_road_speed_limit_offset = get_int_param(self.params, "AutoRoadSpeedLimitOffset", -1)
    self.auto_navi_speed_safety_factor = get_int_param(self.params, "AutoNaviSpeedSafetyFactor", 105) * 0.01

  def update(self, sm) -> bool:
    """每帧调用；返回当帧是否允许 cap 生效（guard∧mode）。"""
    carrot_man = get_carrot_man(sm)
    self.mode_active = self.mode.update(carrot_man is not None)
    self.capping = self.mode_active and carrot_man is not None
    if carrot_man is not None:
      self.desired_speed_kph = float(carrot_man.desiredSpeed)
      self.desired_source = str(carrot_man.desiredSource or "")
      self.n_road_limit_speed = int(carrot_man.nRoadLimitSpeed)
      self.traffic_state = int(carrot_man.trafficState)
      self.active_carrot = int(carrot_man.activeCarrot)
      self.x_dist_to_turn = int(carrot_man.xDistToTurn)
    else:
      self.desired_speed_kph = 0.0
      self.desired_source = ""
      self.n_road_limit_speed = 0
      self.traffic_state = 0
      self.active_carrot = 0
      self.x_dist_to_turn = 0
    # carrot 语义：activeCarrot>1（编码档位）且 100m 内有转向 → ATC 区间
    self.atc_active = self.active_carrot > 1 and 0 < self.x_dist_to_turn < 100
    return self.capping

  def cap_v_target(self, v_target_ms: float) -> float:
    if self.capping and self.desired_speed_kph > 0:
      return min(v_target_ms, self.desired_speed_kph / 3.6)
    return v_target_ms

  def preset_speed_kph(self, fallback_kph: float) -> float:
    """set 拨杆初始化值：carrot 权威期跟随道路限速（×安全系数 或 +偏移）。"""
    if self.mode_active and self.n_road_limit_speed > 0:
      if self.auto_road_speed_limit_offset < 0:
        return self.n_road_limit_speed * self.auto_navi_speed_safety_factor
      return self.n_road_limit_speed + self.auto_road_speed_limit_offset
    return fallback_kph

  def get_carrot_accel(self, v_ego: float) -> float:
    return float(np.interp(v_ego, A_CRUISE_MAX_BP_CARROT, CRUISE_MAX_VALS_CARROT))

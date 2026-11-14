"""导航变道许可闸门（拨杆触发 → 系统执行的导航引导变道）。

数据源为手机 carrotmate 的 carrotNavi（2 Hz，含 laneCurrent / guidanceCurrent /
crossroad）；离线 OSM mapd（LiveMapDataSP）只有限速/路名，无车道/匝道/路口
结构，不参与变道方向性判断。

设计约束（controls-safety 门禁）：
- fail-open：闸门未启用、carrotmate 掉线或数据无效时恒放行，行为与现有
  纯拨杆 ALC 完全一致；
- 只在 DesireHelper 的变道「发起」条件上加一道门（desire_helper.py 的
  preLaneChange → laneChangeStarting），不触碰 BSM 阻断 / 0.5s 清空窗口 /
  ATC 冻结 / 中途 abort 等既有安全门；
- 不改 cereal 消息、不新建进程、不碰 panda 与 CAN 下发。

抑制条件（仅在数据新鲜时生效，全部双向，不判断路线方向冲突）：
- 临近转向/分岔/匝道/环岛/到达（BLOCK_TURN_TYPES，对齐
  carrot_serv.nav_type_mapping group 1-5）且 guidanceCurrent.distanceM
  < NaviLaneChangeTurnDistance（默认 300m）；
- crossroad 可见且 distanceM < NaviLaneChangeCrossroadDistance（默认 150m）。
  crossroad 兜底（#7）：2026-11-14 切高德 SDK 上行后 crossroad 流恒不发
  （spec：高德未提供的流不发送 present=true），该抑制条件曾永久失效；
  现 crossroad 缺席时改用 trafficSignal（红绿灯流，SDK 路径可用）作路口
  临近代理：visible 且 distanceM < NaviLaneChangeSignalDistance（默认 150m）
  即视为临近路口。两条通道任一命中都抑制；
- laneCurrent.count <= 1（单车道）。currentLane 车道边界判断需先实车确认
  carrotmate 车道索引方向（0 为最左还是最右），暂不启用。

方向冲突抑制（#1，默认关，param NaviLaneChangeDirectionEnabled）：
- guidanceCurrent/guidanceNext 的 turnType 经 nav_turn_types.turn_direction()
  归类为明确的 left/right，且距离在 NaviLaneChangeDirectionDistance
  （默认 500m）窗口内，与拨杆方向相反 → 阻断变道发起（如前方 400m 要左转，
  拨右杆不得发起）。同方向、未知方向、掉头、到达均放行；本抑制层不依赖
  laneCurrent 增强字段（车道级 turnInfo 映射等 laneInfoList 真车校准完成
  后再启用，见 spec「车道信息字段映射校准」硬门）。
"""
import time

from openpilot.common.params import Params
from openpilot.common.raw_params import get_int_param
from openpilot.selfdrive.controls.lib.desire_helper import LaneChangeDirection
from openpilot.selfdrive.carrot.nav_turn_types import turn_direction

# carrotNavi 为 2 Hz：3 帧无新数据（约 1.5 s）即视为掉线，闸门放行
CARROT_NAVI_TIMEOUT = 1.5

# carrot_serv.nav_type_mapping 中 group ∈ {1,2,3,4,5} 的原始 turnType 编码
# （1=左右转 2=右转类 3/4=fork/off-ramp 分岔与匝道 5=rotary/uturn/arrive）。
# 与 carrot_serv.nav_type_mapping 保持同步；test_navi_lane_change_gate.py
# 会在测试环境校验两者一致性。
BLOCK_TURN_TYPES = frozenset({
  12, 16, 1000, 1001, 13, 19,
  1002, 1006, 102, 105, 112, 115, 7, 44, 17, 75, 76, 118,
  1003, 1007, 101, 104, 111, 114, 6, 43, 73, 74, 123, 124, 117,
  131, 132, 140, 141, 133, 134, 135, 136, 137, 138, 139, 142, 14, 201,
})


def get_carrot_navi(sm):
  """Return a fresh carrotNavi cap, or None when unavailable (fail-open path).

  carrotNavi 是 2 Hz 常规服务，口径照 carrot_man_input.get_carrot_man：
  seen/alive/valid + recv_time 超时，另加 connected 双保险（carrotmate
  断连但 publisher 心跳仍在发时不得放行闸门进入抑制判定）。
  """
  service = 'carrotNavi'
  # unsubscribed service = no data, same fallback as never-published/stale
  if service not in sm.seen or not (sm.seen[service] and sm.alive[service] and sm.valid[service]):
    return None
  age = time.monotonic() - sm.recv_time[service]
  if not 0.0 <= age <= CARROT_NAVI_TIMEOUT:
    return None
  navi = sm[service]
  return navi if navi.connected else None


class NaviLaneChangeGate:
  """拨杆变道的导航许可闸门：update() 每帧返回是否放行变道发起。"""

  def __init__(self, params: Params | None = None):
    self.params = params if params is not None else Params()
    self.frame = 0
    self.enabled = False
    self.turn_distance = 300
    self.crossroad_distance = 150
    self.signal_distance = 150
    self.direction_enabled = False
    self.direction_distance = 500
    # active=True 表示闸门在线且真实参与判定（供日志/后续 HMI 使用）
    self.active = False
    self.block_reason = ""
    self._read_params()

  def _read_params(self):
    self.enabled = self.params.get_bool("NaviGuidedLaneChangeEnabled")
    self.turn_distance = max(0, get_int_param(self.params, "NaviLaneChangeTurnDistance", 300))
    self.crossroad_distance = max(0, get_int_param(self.params, "NaviLaneChangeCrossroadDistance", 150))
    self.signal_distance = max(0, get_int_param(self.params, "NaviLaneChangeSignalDistance", 150))
    self.direction_enabled = self.params.get_bool("NaviLaneChangeDirectionEnabled")
    self.direction_distance = max(0, get_int_param(self.params, "NaviLaneChangeDirectionDistance", 500))

  def update(self, sm, direction: int = LaneChangeDirection.none) -> bool:
    """每帧调用（20 Hz）；返回当帧是否放行变道发起（fail-open）。

    direction 为拨杆请求方向（LaneChangeDirection.left/right），仅用于
    方向冲突抑制；不传或为 none 时跳过该层（其余抑制条件照常生效）。
    """
    self.frame += 1
    if self.frame % 100 == 0:
      self._read_params()

    self.active, self.block_reason = False, ""
    if not self.enabled:
      return True

    navi = get_carrot_navi(sm)
    if navi is None:
      # carrotmate 掉线 / 无数据：退回现有纯拨杆 ALC 行为，不抑制
      self.block_reason = "navi_unavailable"
      return True

    reason = self._block_reason(navi, direction)
    if reason:
      self.active, self.block_reason = True, reason
      return False
    self.active = True
    return True

  def _near_crossroad(self, navi) -> bool:
    """crossroad 流命中，或 crossroad 缺席时以 trafficSignal 兜底（#7）。"""
    crossroad = navi.crossroad
    if crossroad.meta.present and crossroad.visible and \
        0 < crossroad.distanceM < self.crossroad_distance:
      return True
    # 高德 SDK 上行不发 crossroad：红绿灯流可见即临近路口（任一灯态都算）
    signal = navi.trafficSignal
    if not crossroad.meta.present and signal.meta.present and signal.visible and \
        0 < signal.distanceM < self.signal_distance:
      return True
    return False

  def _direction_conflict(self, navi, direction: int) -> bool:
    """拨杆方向与导航要求的转向方向相反（#1，独立开关）。"""
    if direction not in (LaneChangeDirection.left, LaneChangeDirection.right):
      return False
    requested = "left" if direction == LaneChangeDirection.left else "right"
    for guidance in (navi.guidanceCurrent, navi.guidanceNext):
      if not guidance.meta.present or not 0 < guidance.distanceM < self.direction_distance:
        continue
      needed = turn_direction(guidance.turnType)
      if needed and needed != requested:
        return True
    return False

  def _block_reason(self, navi, direction: int = LaneChangeDirection.none) -> str:
    lane = navi.laneCurrent
    if lane.meta.present and 0 < lane.count <= 1:
      return "single_lane"
    guidance = navi.guidanceCurrent
    if guidance.meta.present and guidance.turnType in BLOCK_TURN_TYPES and \
        0 < guidance.distanceM < self.turn_distance:
      return "near_turn"
    if self._near_crossroad(navi):
      return "near_crossroad"
    if self.direction_enabled and self._direction_conflict(navi, direction):
      return "direction_conflict"
    return ""

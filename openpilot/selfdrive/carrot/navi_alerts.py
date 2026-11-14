"""carrotNavi → 车机导航预警（#2 红绿灯接近 / #5 转向预告 / 偏航提示）。

selfdrived 每帧调用 NaviAlertTracker.update(sm)，命中的事件经 events_sp
进入 AlertManager（UI + 声音自动生效）；预警文案在 EVENTS_SP 的 Alert
回调里从 sm['carrotNavi'] 现算，保证 2Hz 数据驱动而非 100Hz 快照陈旧。

设计约束（controls-safety 门禁）：
- fail-open：carrotmate 掉线或数据无效时不出任何事件，UI 行为与现状一致；
- 只出视觉预警（AudibleAlert.none），不改纵向/横向控制，不碰既有安全门；
- 三类预警同时命中时只出一条（偏航 > 红灯 > 转向预告），避免刷屏；
- 红灯预警仅在本车接近时（vEgo > 3 m/s）出现，等灯时不打扰。
"""
import time

from openpilot.cereal import custom
from openpilot.selfdrive.carrot.nav_turn_types import nav_type_mapping

# carrotNavi 是 2 Hz：3 帧无新数据（约 1.5 s）即视为掉线，口径对齐
# navi_lane_change_gate.CARROT_NAVI_TIMEOUT（复制而非导入：该模块拉入
# desire_helper，selfdrived 不需要）
CARROT_NAVI_TIMEOUT = 1.5

TRAFFIC_SIGNAL_DISTANCE_MAX = 300  # m，红灯预警距离窗
NAV_TURN_DISTANCE_MIN = 150  # m，<150m 属 ATC 转弯区间 / 闸门抑制管辖，不再预告
NAV_TURN_DISTANCE_MAX = 800  # m，预告窗口上限
NAV_TURN_MIN_SPEED = 3.0  # m/s，静止/蠕动时不预告转向

EventNameSP = custom.OnroadEventSP.EventName

# nav_type_mapping (navType, modifier) → 中文短文案；映射外的组合忽略
NAV_TURN_TEXT = {
  ("turn", "left"): "左转",
  ("turn", "sharp left"): "急左转",
  ("turn", "slight left"): "稍向左",
  ("turn", "right"): "右转",
  ("turn", "sharp right"): "急右转",
  ("turn", "slight right"): "稍向右",
  ("fork", "left"): "前方分岔靠左",
  ("fork", "slight left"): "前方分岔靠左",
  ("fork", "right"): "前方分岔靠右",
  ("fork", "slight right"): "前方分岔靠右",
  ("off ramp", "left"): "驶出匝道",
  ("off ramp", "slight left"): "驶出匝道",
  ("off ramp", "right"): "驶出匝道",
  ("off ramp", "slight right"): "驶出匝道",
  ("rotary", "left"): "进入环岛",
  ("rotary", "slight left"): "进入环岛",
  ("rotary", "sharp left"): "进入环岛",
  ("rotary", "right"): "进入环岛",
  ("rotary", "slight right"): "进入环岛",
  ("rotary", "sharp right"): "进入环岛",
  ("rotary", "straight"): "环岛直行",
  ("turn", "uturn"): "掉头",
  ("arrive", "straight"): "到达目的地",
}


def _navi_fresh(sm) -> bool:
  """carrotNavi 新鲜度口径与 navi_lane_change_gate.get_carrot_navi 一致。"""
  service = 'carrotNavi'
  if service not in sm.seen or not (sm.seen[service] and sm.alive[service] and sm.valid[service]):
    return False
  age = time.monotonic() - sm.recv_time[service]
  if not 0.0 <= age <= CARROT_NAVI_TIMEOUT:
    return False
  return bool(sm[service].connected)


def turn_text(turn_type: int) -> str:
  """turnType → 中文短文案（转向预告/播报共用）；映射外返回 ''。"""
  entry = nav_type_mapping.get(turn_type)
  if entry is None:
    return ""
  return NAV_TURN_TEXT.get((entry[0], entry[1]), "")


class NaviAlertTracker:
  """从 carrotNavi 提炼当帧应展示的导航预警事件（至多一条）。"""

  def __init__(self):
    self.event = None  # EventNameSP enumerant，None = 本帧无预警

  def update(self, sm) -> None:
    self.event = None
    if not _navi_fresh(sm):
      return
    navi = sm['carrotNavi']

    # 1) 偏航：导航重新规划中，提示用户（闸门侧随 guidance 断流自动 fail-open）
    status = navi.navigationStatus
    if status.meta.present and status.offRoute:
      self.event = EventNameSP.carrotNavOffRoute
      return

    v_ego = sm['carState'].vEgo

    # 2) 红灯接近：可见且红灯亮、距离在窗口内、本车在接近
    #    （距离口径与闸门一致：严格小于，等于阈值不触发）
    signal = navi.trafficSignal
    if v_ego > 3.0 and signal.meta.present and signal.visible and signal.redValid and \
        signal.redOn and 0 < signal.distanceM < TRAFFIC_SIGNAL_DISTANCE_MAX:
      self.event = EventNameSP.carrotTrafficSignal
      return

    # 3) 转向预告：guidanceCurrent 转向已知且距离在窗口内
    guidance = navi.guidanceCurrent
    if v_ego > NAV_TURN_MIN_SPEED and guidance.meta.present and \
        NAV_TURN_DISTANCE_MIN < guidance.distanceM <= NAV_TURN_DISTANCE_MAX and \
        turn_text(guidance.turnType):
      self.event = EventNameSP.carrotNavTurn

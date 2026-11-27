"""carrotNavi → 车机导航预警（#2 红绿灯接近 / #5 转向预告 / 偏航提示）。

selfdrived 每帧调用 NaviAlertTracker.update(sm, v_ego)，命中的事件经 events_sp
进入 AlertManager（UI + 声音自动生效）；预警文案在 EVENTS_SP 的 Alert
回调里从 sm['carrotNavi'] 现算，保证 2Hz 数据驱动而非 100Hz 快照陈旧。

注意：selfdrived 的 SubMaster 不订阅 carState（carState 走独立 raw sock），
sm['carState'] 会 KeyError——2026-11-22 实车日志确认 mate 推导航时该路径
导致 selfdrived 崩溃循环，故 v_ego 必须显式传入，本模块禁止访问 sm['carState']。

设计约束（controls-safety 门禁）：
- fail-open：carrotmate 掉线或数据无效时不出任何事件，UI 行为与现状一致；
- 只出视觉预警（AudibleAlert.none），不改纵向/横向控制，不碰既有安全门；
- 多类预警同时命中时只出一条，优先级：
  偏航 > 红灯 > 连续转弯 > 即将到达 > 转向预告，避免刷屏；
- 红灯预警仅在本车接近时（vEgo > 3 m/s）出现，等灯时不打扰；
- 连续转弯/即将到达为一次性预警（armed 语义）：每个武装周期只响一次，
  路线状态翻转（重算路/新路线/偏航边沿）后重新武装——翻转只可能移除
  武装（允许再响），永不压制，fail-safe by construction（对齐源库
  tc-v61 probe 的 generation 翻转 re-arm 模式；本地 generation 是全量
  状态计数器，不能直接当"重算路"信号，见 _route_epoch 注释）。
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
CONSECUTIVE_TURN_NEXT_DISTANCE_MAX = 400  # m，下一 maneuver 连续转弯预告窗
ARRIVAL_SOON_DISTANCE_MAX = 500  # m，即将到达提醒距离阈值

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


def is_turn_family(turn_type: int) -> bool:
  """turnType 是否属 turn/rotary 家族（连续转弯预警的判定口径）。

  未知编码 fail-inert：映射查不到一律 False，绝不臆测（对齐源库
  "未知 turnCode 永远 fail-inert" 纪律）。掉头属 turn 家族；fork/off ramp/
  arrive/notification 不算。
  """
  entry = nav_type_mapping.get(turn_type)
  return entry is not None and entry[0] in ("turn", "rotary")


class NaviAlertTracker:
  """从 carrotNavi 提炼当帧应展示的导航预警事件（至多一条）。"""

  def __init__(self):
    self.event = None  # EventNameSP enumerant，None = 本帧无预警
    # 路线状态 epoch：任一分量翻转视为"路线变了"，一次性预警重新武装
    self._route_epoch = None
    self._consecutive_turns_armed = True
    self._arrival_armed = True

  def _rearm_if_route_changed(self, navi) -> None:
    """路线状态翻转 → 重新武装全部一次性预警。

    本地 CarrotNaviState.generation 是接收端"任意状态更新"计数器
    （carrot_navi._mark_state_changed_locked 每帧更新都 +1），不能像源库
    tc-v61 探针那样直接当 OnMainRouteChanged/RouteDeleted 信号用——否则
    每帧都 re-arm，一次性语义名存实亡。这里改用可观测的路线状态三元组：
    route.present / routePresent / offRoute 任一边沿翻转 ≈ 重算路或路线
    消失/恢复。翻转只会 re-arm（允许再响），永不压制——移除抑制方向。
    """
    epoch = (
      bool(navi.route.meta.present),
      bool(navi.navigationStatus.routePresent),
      bool(navi.navigationStatus.offRoute),
    )
    if self._route_epoch is None:
      self._route_epoch = epoch  # 首帧建基线，不触发 re-arm
      return
    if epoch != self._route_epoch:
      self._route_epoch = epoch
      self._consecutive_turns_armed = True
      self._arrival_armed = True

  def _consecutive_turns_hit(self, navi) -> bool:
    """当前 + 下一 maneuver 均 turn/rotary 家族，且下一 maneuver 在窗口内。"""
    cur, nxt = navi.guidanceCurrent, navi.guidanceNext
    if not (cur.meta.present and nxt.meta.present):
      return False
    if not 0 < nxt.distanceM <= CONSECUTIVE_TURN_NEXT_DISTANCE_MAX:
      return False
    # 任一 maneuver 分类失败（未知编码）→ 不发，绝不误发（fail-inert）
    return is_turn_family(cur.turnType) and is_turn_family(nxt.turnType)

  def _arrival_hit(self, navi) -> bool:
    """引导中剩余里程首次进入阈值：route 在场 + 引导激活 + ≤500m。"""
    route = navi.route
    status = navi.navigationStatus
    return bool(
      route.meta.present
      and status.guidanceActive
      and 0 < route.remainingDistanceM <= ARRIVAL_SOON_DISTANCE_MAX
    )

  def update(self, sm, v_ego: float) -> None:
    """v_ego 由调用方显式传入（selfdrived 的 SM 无 carState，见模块头注释）。"""
    self.event = None
    if not _navi_fresh(sm):
      return
    navi = sm['carrotNavi']
    self._rearm_if_route_changed(navi)

    # 1) 偏航：导航重新规划中，提示用户（闸门侧随 guidance 断流自动 fail-open）
    status = navi.navigationStatus
    if status.meta.present and status.offRoute:
      self.event = EventNameSP.carrotNavOffRoute
      return

    # 2) 红灯接近：可见且红灯亮、距离在窗口内、本车在接近
    #    （距离口径与闸门一致：严格小于，等于阈值不触发）
    signal = navi.trafficSignal
    if v_ego > 3.0 and signal.meta.present and signal.visible and signal.redValid and \
        signal.redOn and 0 < signal.distanceM < TRAFFIC_SIGNAL_DISTANCE_MAX:
      self.event = EventNameSP.carrotTrafficSignal
      return

    # 3) 连续转弯（一次性）：提前准备连续转弯，优先于单条转向预告
    #    （一次性预警若排在连续预警之后会被永久压住，永远轮不到展示）
    if self._consecutive_turns_armed and self._consecutive_turns_hit(navi):
      self._consecutive_turns_armed = False
      self.event = EventNameSP.carrotNavConsecutiveTurns
      return

    # 4) 即将到达（一次性）：纯提醒，不自动减速
    if self._arrival_armed and self._arrival_hit(navi):
      self._arrival_armed = False
      self.event = EventNameSP.carrotNavArrivalSoon
      return

    # 5) 转向预告：guidanceCurrent 转向已知且距离在窗口内（连续预警）
    guidance = navi.guidanceCurrent
    if v_ego > NAV_TURN_MIN_SPEED and guidance.meta.present and \
        NAV_TURN_DISTANCE_MIN < guidance.distanceM <= NAV_TURN_DISTANCE_MAX and \
        turn_text(guidance.turnType):
      self.event = EventNameSP.carrotNavTurn

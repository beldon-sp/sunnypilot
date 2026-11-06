"""
mate 事件源：订阅 cereal 总线，推导边沿事件与周期状态帧并交给广播器（server.py）。

  events  onroadEvents 集合变化   {"added", "removed", "active"}
  opstate selfdriveState 状态机跳转 {"state", "enabled", "active", "engageable"}
  button  方向盘按键边沿           {"button", "pressed"}
  sound   播报声音变化             {"sound", "file", "loop", "alertText1", "alertText2"}
  frame   2Hz 周期状态帧           {"car"?, "radar"?, "carrot"?}：驾驶屏连续值，段与快照同源；
          窗口内 carState/radarState/carrotMan 均无更新（offroad）则不推；
          carrot 段为协议 v4 新增（carrotMan 遥测）

v3 起不再有 navi kind：其输入 carrotNavi 即 carrotmate（客户端）经 7706/7714 上行
推送的导航数据，回推属回声——服务定位原则：mate 只推 op 独有数据，客户端自产的
数据不回推。

除边沿事件与 frame 外，poller 还维护一份最新状态快照，供 server.py 的 GET /state 与
WS snapshot 请求做客户端初始化，避免客户端为了拿“当前值”而重引入周期推流：

  car/radar 每帧刷新（与 frame 的同名段共用同一构建函数）
  events/opstate/sound  随边沿更新
  meta      每轮心跳（frame 计数 + onroad 标志）——快照整体变旧 = poller 挂了
  services  每轮 SubMaster 健康度——topic seen=false = 数据源进程没跑（offroad 常态）
  panda     随 pandaStates 更新——pandad always_run，offroad 下唯一的活数据源

参考实现是 cp_byd local_collector/collector.py；其 10Hz 周期帧不移植（frame 为
2Hz 状态语义），admin API 也不在 mate 范围内。
"""
import time
from functools import cache
from typing import Optional

import openpilot.cereal.messaging as messaging
from openpilot.cereal import car, custom, log
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.car.openpilot_toggle import CruiseMainOpenpilotToggle
from openpilot.selfdrive.ui.soundd import (
  AudibleAlert,
  check_selfdrive_timeout_alert,
  sound_list,
)

ButtonType = car.CarState.ButtonEvent.Type

SERVICES = ["carState", "selfdriveState", "selfdriveStateSP", "onroadEvents", "carrotMan", "radarState", "pandaStates"]

EVENT_FLAGS = ("enable", "noEntry", "warning", "userDisable", "softDisable",
               "immediateDisable", "preEnable", "permanent",
               "overrideLateral", "overrideLongitudinal")

NONE_ALERT = int(AudibleAlert.none)
MADS_PAUSED_STATE = custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState.paused
MS_TO_KPH = 3.6

# frame 周期状态帧：驾驶屏连续值刷新率；窗口内无新数据即整帧跳过（offroad 自然停推）
FRAME_HZ = 2
FRAME_PERIOD_S = 1.0 / FRAME_HZ


@cache
def _enum_map(enum_type) -> dict[int, str]:
  return {v: k for k, v in enum_type.schema.enumerants.items()}


def enum_name(enum_type, value) -> str:
  try:
    return _enum_map(enum_type).get(int(value), "unknown")
  except Exception:
    return "unknown"


# ---------------- 纯函数：事件推导 ----------------

def event_to_dict(e) -> dict:
  out = {"name": enum_name(log.OnroadEvent.EventName, e.name.raw)}
  for f in EVENT_FLAGS:
    out[f] = bool(getattr(e, f))
  return out


def diff_events(prev: dict[str, dict], current: list[dict]) -> tuple[dict[str, dict], dict | None]:
  """返回 (新状态, payload)；集合无变化时 payload 为 None（去抖，不每帧推）。"""
  cur_map = {e["name"]: e for e in current}
  added = sorted(cur_map.keys() - prev.keys())
  removed = sorted(prev.keys() - cur_map.keys())
  if not added and not removed:
    return prev, None
  return cur_map, {"added": added, "removed": removed, "active": list(cur_map.values())}


def opstate_payload(ss) -> dict:
  return {
    "state": enum_name(log.SelfdriveState.OpenpilotState, ss.state.raw),
    "enabled": ss.enabled,
    "active": ss.active,
    "engageable": ss.engageable,
  }


def button_payloads(cs) -> list[dict]:
  # CarState.buttonEvents 本身就是变化沿列表（按下/松开各一条），只在 updated 帧消费
  return [{"button": enum_name(ButtonType, b.type.raw), "pressed": b.pressed}
          for b in cs.buttonEvents]


def lat_enabled(sm) -> bool:
  """本仓 CarState 无 latEnabled（carrot 扩展字段）：按本仓 MADS 语义推导——
  MADS 可用时 lat = enabled 且非 paused；否则回落 selfdriveState.active。"""
  try:
    mads = sm["selfdriveStateSP"].mads
    if mads.available:
      return bool(mads.enabled and mads.state != MADS_PAUSED_STATE)
  except Exception:
    pass
  return bool(sm["selfdriveState"].active)


def car_snapshot(cs, sm) -> dict:
  """GET /state 的 car 段：当前车辆状态快照（非周期推流），字段对照本仓 car.capnp。
  与 frame 的 car 段同源同函数（一个拉一个推）。
  本仓 CarState 无 speedLimit/engineRpm/carrotCruise/gas/brake 字段：speedLimit
  类键恒 0（BYD 本就不填，限速显示走 frame.carrot 段），gas/brake 浮点恒 0（本仓
  只有 gasPressed/brakePressed 布尔），键位保留以稳定客户端帧结构。"""
  cruise = cs.cruiseState
  return {
    "vEgo": round(cs.vEgo, 3),
    "vEgoKph": round(cs.vEgo * MS_TO_KPH, 1),
    "vEgoCluster": round(cs.vEgoCluster, 3),
    "aEgo": round(cs.aEgo, 3),
    "standstill": cs.standstill,
    "gearShifter": enum_name(car.CarState.GearShifter, cs.gearShifter.raw),
    "gas": 0.0,
    "gasPressed": cs.gasPressed,
    "brake": 0.0,
    "brakePressed": cs.brakePressed,
    "steeringAngleDeg": round(cs.steeringAngleDeg, 2),
    "steeringPressed": cs.steeringPressed,
    "cruiseState": {
      "enabled": cruise.enabled,
      "available": cruise.available,
      "speed": round(cruise.speed, 3),
      "speedKph": round(cruise.speed * MS_TO_KPH, 1),
      "speedCluster": round(cruise.speedCluster, 3),
      "standstill": cruise.standstill,
      "nonAdaptive": cruise.nonAdaptive,
    },
    "vCruise": round(cs.vCruise, 3),
    "vCruiseKph": round(cs.vCruise * MS_TO_KPH, 1),
    "leftBlinker": cs.leftBlinker,
    "rightBlinker": cs.rightBlinker,
    "leftBlindspot": cs.leftBlindspot,
    "rightBlindspot": cs.rightBlindspot,
    "doorOpen": cs.doorOpen,
    "seatbeltUnlatched": cs.seatbeltUnlatched,
    "parkingBrake": cs.parkingBrake,
    "brakeHoldActive": cs.brakeHoldActive,
    "latEnabled": lat_enabled(sm),
    "speedLimit": 0.0,
    "speedLimitKph": 0.0,
    "engineRpm": 0.0,
    "accFaulted": cs.accFaulted,
    "espDisabled": cs.espDisabled,
    "canValid": cs.canValid,
    "canTimeout": cs.canTimeout,
    "carrotCruise": False,
    "fuelGauge": round(cs.fuelGauge, 3),
    "charging": cs.charging,
  }


def _empty_lead() -> dict:
  """本仓 radarState 只有 leadOne/leadTwo（无 carrot 的 leadLeft/leadRight 扩展）：
  侧向键恒为空 lead，键位保留以稳定客户端帧结构。"""
  return {"status": False, "dRel": 0.0, "yRel": 0.0, "vRel": 0.0, "aRel": 0.0,
          "vLead": 0.0, "dPath": 0.0, "fcw": False, "trackId": -1}


def radar_snapshot(rs) -> dict:
  """radarState → radar 快照段 / frame radar 段（JSON-able）。字段名与 carrotmate
  Lead 模型一致（其 trackId 对应 cereal radarTrackId）；radarErrors 为非空错误名列表
  （Car.RadarData.Error 是四个 Bool 的 struct）。
  本仓 LeadData 无 dPath 字段，恒 0。"""
  def lead(l) -> dict:
    return {
      # 本仓 LeadData 的目标在位标志叫 present（carrot/旧版叫 status）
      "status": bool(l.present),
      "dRel": round(l.dRel, 2),
      "yRel": round(l.yRel, 2),
      "vRel": round(l.vRel, 2),
      "aRel": 0.0,  # 本仓无 aRel
      "vLead": round(l.vLead, 2),
      "dPath": 0.0,  # 本仓无 dPath
      "fcw": False,  # 本仓无 fcw
      "trackId": int(l.radarTrackId),
    }

  err = rs.radarErrors
  return {
    "leadOne": lead(rs.leadOne),
    "leadTwo": lead(rs.leadTwo),
    "leadLeft": _empty_lead(),
    "leadRight": _empty_lead(),
    "leadsCenterCount": 0,
    "radarErrors": [name for name, on in (
      ("canError", err.canError), ("radarFault", err.radarFault),
      ("wrongConfig", err.wrongConfig),
      ("radarUnavailableTemporary", err.radarUnavailableTemporary)) if on],
  }


# ---------------- frame：周期状态帧 ----------------

def carrot_snapshot(cm) -> dict:
  """carrotMan → frame carrot 段（协议 v4 新增，JSON-able）。字段名与 cereal
  carrotMan 一致，源数据即 carrot_man 20Hz 聚合输出。"""
  return {
    "nRoadLimitSpeed": int(cm.nRoadLimitSpeed),
    "xSpdLimit": int(cm.xSpdLimit),
    "xSpdDist": int(cm.xSpdDist),
    "xSpdCountDown": int(cm.xSpdCountDown),
    "xDistToTurn": int(cm.xDistToTurn),
    "xTurnCountDown": int(cm.xTurnCountDown),
    "desiredSpeed": int(cm.desiredSpeed),
    "desiredSource": str(cm.desiredSource or ""),
    "szPosRoadName": str(cm.szPosRoadName or ""),
    "leftSec": int(cm.leftSec),
    "trafficState": int(cm.trafficState),
  }


def frame_payload(car: dict | None, radar: dict | None, carrot: dict | None = None) -> dict | None:
  """frame 帧组装（纯函数）：仅出现有数据的段；各段全空（窗口无更新）返回 None。"""
  if car is None and radar is None and carrot is None:
    return None
  out: dict = {}
  if car is not None:
    out["car"] = car
  if radar is not None:
    out["radar"] = radar
  if carrot is not None:
    out["carrot"] = carrot
  return out


class FrameTicker:
  """frame 周期调度（纯逻辑，时钟注入便于测试）：到点检查窗口内是否有段更新，
  有则出帧并清空窗口（next tick 重新积累），无更新返回 None——offroad 数据源
  停止后自然停推。节拍落后（轮询阻塞）时按 period 推进而非追赶积压。"""

  def __init__(self, period_s: float, now: float) -> None:
    self.period_s = period_s
    self.next_t = now + period_s
    self.car: Optional[dict] = None
    self.radar: Optional[dict] = None
    self.carrot: Optional[dict] = None

  def tick(self, now: float) -> Optional[dict]:
    if now < self.next_t:
      return None
    self.next_t = max(self.next_t + self.period_s, now + self.period_s)
    payload = frame_payload(self.car, self.radar, self.carrot)
    self.car = self.radar = self.carrot = None
    return payload


def services_status(sm) -> dict:
  """GET /state 的 services 段：SubMaster 视角的各 topic 健康度。
  seen=启动以来是否收到过（frequency=0 的 on-demand topic 恒判 alive，
  全靠 seen 区分“没跑”与“跑了但不新鲜”）；ageS 用本进程单调钟差值，
  logMonoTime 同为 CLOCK_MONOTONIC，仅同机同次开机内可比。"""
  now_ns = time.monotonic_ns()
  out = {}
  for s in sm.services:
    seen = bool(sm.seen[s])
    out[s] = {"seen": seen,
              "alive": bool(sm.alive[s]),
              "valid": bool(sm.valid[s]),
              "ageS": round(max(0.0, (now_ns - sm.logMonoTime[s]) / 1e9), 2) if seen else None}
  return out


def panda_state_to_dict(p) -> dict:
  can_states = [{"bus": i, "busOff": c.busOff, "busOffCnt": c.busOffCnt,
                 "errorWarning": c.errorWarning, "errorPassive": c.errorPassive,
                 "canSpeed": c.canSpeed, "totalRxCnt": c.totalRxCnt, "totalTxCnt": c.totalTxCnt}
                for i, c in enumerate((p.canState0, p.canState1, p.canState2))]
  return {
    "type": enum_name(log.PandaState.PandaType, p.pandaType.raw),
    "heartbeatLost": p.heartbeatLost,
    "ignitionLine": p.ignitionLine,
    "controlsAllowed": p.controlsAllowed,
    "safetyModel": enum_name(car.CarParams.SafetyModel, p.safetyModel.raw),
    "safetyParam": p.safetyParam,
    "faultStatus": enum_name(log.PandaState.FaultStatus, p.faultStatus.raw),
    "faults": [enum_name(log.PandaState.FaultType, f.raw) for f in p.faults],
    "harnessStatus": enum_name(log.PandaState.HarnessStatus, p.harnessStatus.raw),
    "uptime": p.uptime,
    "canStates": can_states,
  }


def panda_payload(panda_states) -> dict:
  """GET /state 的 panda 段：台架/车上 panda 是否在线、CAN 收发是否活着，
  offroad 也持续更新（pandad always_run）。"""
  return {"count": len(panda_states), "pandas": [panda_state_to_dict(p) for p in panda_states]}


def sound_payload(alert: int, ss) -> dict:
  entry = sound_list.get(alert)
  return {
    "sound": enum_name(AudibleAlert, alert),
    "file": entry[0] if entry else None,
    "loop": entry[1] is None if entry else False,
    "alertText1": ss.alertText1,
    "alertText2": ss.alertText2,
  }


class SoundTracker:
  """复刻 soundd 的 alert 推导链（soundd.py update_alert / get_audible_alert），
  加上 carrot leftSec 倒计时告警（carrot soundd update_carrot_alert），但没有音频
  时钟：转入 none 立即上报，不等有限长音效播完，客户端只会提前停止播放。

  本仓 AudibleAlert 无 audio1-10/longDisengaged 成员：倒计时直接按文件名下发
  （wav 随树分发，/sounds/<file>.wav 可取），11s 的提前提示映射到本仓
  promptDistracted。发过文件 payload 后 alert 置 CARROT_FILE_ALERT 哨兵，下一轮
  alertSound==none 与哨兵不同 → 照常补发一条 sound=none 让客户端停播（与 carrot
  每秒「beep 后 none」的节奏一致）。"""

  CARROT_FILE_ALERT = -1  # 哨兵：当前"告警"是 carrot 倒计时文件，非枚举成员

  def __init__(self):
    self.alert = NONE_ALERT
    self.carrot_count_down = 0  # 与 soundd 一致的初值，静止期不误发 longDisengaged
    self.selfdrive_timeout_alert = False

  def _carrot_countdown_payload(self, left_sec: int, ss) -> dict | None:
    if left_sec == 0:
      sound, file, loop = "longDisengaged", "tici_disengaged.wav", True
    elif 0 < left_sec <= 10:
      sound, file, loop = f"audio{left_sec}", f"audio_{left_sec}.wav", False
    else:
      return None
    return {"sound": sound, "file": file, "loop": loop,
            "alertText1": ss.alertText1, "alertText2": ss.alertText2}

  def update(self, sm, main_toggle: bool) -> dict | None:
    """每轮 SubMaster 更新后调用；alert 变化时返回 sound payload，否则 None。"""
    ss = sm["selfdriveState"]
    file_override: str | None = None
    if main_toggle:
      new_alert = int(AudibleAlert.prompt)
    elif sm.updated["selfdriveState"] or sm.updated["carrotMan"]:
      new_alert = int(ss.alertSound.raw)
      # soundd 只在 alertSound==none 时用 carrot 倒计时覆盖
      if new_alert == NONE_ALERT:
        left_sec = int(sm["carrotMan"].leftSec)
        if self.carrot_count_down != left_sec:
          self.carrot_count_down = left_sec
          if left_sec == 11:
            new_alert = int(AudibleAlert.promptDistracted)
          else:
            countdown = self._carrot_countdown_payload(left_sec, ss)
            if countdown is not None:
              self.alert = self.CARROT_FILE_ALERT
              return countdown
    elif check_selfdrive_timeout_alert(sm):
      self.selfdrive_timeout_alert = True
      new_alert = int(AudibleAlert.warningImmediate)
    elif self.selfdrive_timeout_alert:
      self.selfdrive_timeout_alert = False
      new_alert = NONE_ALERT
    else:
      return None

    if new_alert != NONE_ALERT and new_alert not in sound_list:
      cloudlog.error(f"mate received unsupported alert {new_alert}")
      new_alert = NONE_ALERT
    if new_alert == self.alert:
      return None
    self.alert = new_alert
    return sound_payload(new_alert, ss)


# ---------------- 采集线程 ----------------

class MatePoller:
  def __init__(self, emit, set_state=None):
    self.emit = emit  # emit(kind: str, data: dict)，server.Hub 提供，线程安全
    self.set_state = set_state  # set_state(kind, data)，维护 /state 快照，可选

  def run(self):
    sm = messaging.SubMaster(SERVICES)
    params = Params()
    events_map: dict[str, dict] = {}
    last_opstate: dict | None = None
    tracker = SoundTracker()
    cruise_main_toggle = CruiseMainOpenpilotToggle(ButtonType.mainCruise)
    ticker = FrameTicker(FRAME_PERIOD_S, time.monotonic())

    cloudlog.info("mate poller started")
    while True:
      sm.update(100)  # 与参考 collector 一致：有消息即返回，空闲最多 100ms 一轮
      now = time.monotonic()

      # 每轮心跳 + 总线健康度：/state 的 data 为空时，客户端靠这两段自查——
      # meta ts 变旧=poller 线程挂了；services 全 seen=false=offroad 没数据源。
      if self.set_state is not None:
        # 本仓 manager 维护 IsOffroad（carrot 树是 IsOnroad）
        self.set_state("meta", {"frame": sm.frame, "onroad": not params.get_bool("IsOffroad")})
        self.set_state("services", services_status(sm))
        if sm.updated["pandaStates"]:
          self.set_state("panda", panda_payload(sm["pandaStates"]))

      if sm.updated["onroadEvents"]:
        events_map, payload = diff_events(events_map, [event_to_dict(e) for e in sm["onroadEvents"]])
        if payload:
          self.emit("events", payload)

      if sm.updated["selfdriveState"]:
        payload = opstate_payload(sm["selfdriveState"])
        if payload != last_opstate:
          last_opstate = payload
          self.emit("opstate", payload)

      # car/radar：快照段与 frame 窗口同源同帧——快照取全量，frame 只取窗口内更新。
      # radarState 100Hz，onroad 下 poller 每轮（~10ms）都 updated，随轮刷新即可
      # （快照写入与 frame 窗口赋值都是 O(1)，与既有 meta/services 每轮刷新同模式）。
      if sm.updated["carState"]:
        cs = sm["carState"]
        car = car_snapshot(cs, sm)
        if self.set_state is not None:
          self.set_state("car", car)
        ticker.car = car
        for payload in button_payloads(cs):
          self.emit("button", payload)

      if sm.updated["radarState"]:
        radar = radar_snapshot(sm["radarState"])
        if self.set_state is not None:
          self.set_state("radar", radar)
        ticker.radar = radar

      # carrot 遥测：20Hz 输入随轮喂 frame 窗口（协议 v4 carrot 段）
      if sm.updated["carrotMan"]:
        ticker.carrot = carrot_snapshot(sm["carrotMan"])

      payload = ticker.tick(now)
      if payload:
        self.emit("frame", payload)

      # 声音：与 soundd 主循环一致——每轮先过 main toggle 短路，再做推导。
      # update() 必须在拿到新 carState 后立刻调用，长按计时依赖逐帧喂同一批事件。
      toggled = cruise_main_toggle.update(sm["carState"].buttonEvents, sm["selfdriveState"].enabled)
      payload = tracker.update(sm, toggled)
      if payload:
        self.emit("sound", payload)

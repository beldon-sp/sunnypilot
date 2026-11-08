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

协议 v5 起 /state 与 WS snapshot 扩展 18 个纯快照段（只进快照，不广播、不补发，
机制与 meta/services/panda 相同，段未收到过数据不出现）：

  device←deviceState            peripheral←peripheralState     manager←managerState
  proc←procLog                  backup←backupManagerSP         temp←temperatureSensor
  carParams←carParams+SP        controls←controlsState         carSP←carStateSP
  control←carControl+carControlSP   plan←longitudinalPlan+SP+lateralManeuverPlan
  loc←liveLocationKalman        gps←gpsLocationExternal        map←liveMapDataSP
  model←modelV2+modelDataV2SP   dmodel←drivingModelData        odom←cameraOdometry
  calib←extrinsicsCalibration   params←lateralTorqueParameters+lateralDelay+vehicleParameters

均遵守服务定位原则：数据源全部是 op 内部推导/聚合结果，局域网客户端无法自产。

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

SERVICES = [
  # 事件与状态推导骨干（v1 起即为 frame/边沿事件数据源）
  "carState", "selfdriveState", "selfdriveStateSP", "onroadEvents", "carrotMan", "radarState", "pandaStates",
  # v5 快照段：设备/系统状态
  "deviceState", "peripheralState", "managerState", "procLog", "temperatureSensor", "backupManagerSP",
  # v5 快照段：车辆/控制
  "carParams", "carParamsSP", "controlsState", "carStateSP", "carControl", "carControlSP",
  "longitudinalPlan", "longitudinalPlanSP", "lateralManeuverPlan",
  # v5 快照段：定位/地图
  "liveLocationKalman", "gpsLocationExternal", "liveMapDataSP",
  # v5 快照段：模型/感知
  "modelV2", "modelDataV2SP", "drivingModelData", "cameraOdometry",
  # v5 快照段：标定/参数
  "extrinsicsCalibration", "lateralTorqueParameters", "lateralDelay", "vehicleParameters",
]

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
    # vCruise 本身即 kph（card.py 取自 VCruiseHelper 的 v_cruise_kph，V_CRUISE_* 均为
    # kph、未设定=255），不是 m/s——勿乘 MS_TO_KPH（真车实测 30kph 曾被放大成 108）。
    "vCruiseKph": round(cs.vCruise, 1),
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


# ---------------- 快照段（协议 v5 新增）：纯快照，不广播、不补发 ----------------
#
# 与 meta/services/panda 同机制：set_state 只进 /state 与 WS snapshot。数据源均为
# op 内部推导/聚合结果（局域网客户端无法自产），段未收到过数据则不出现。
# 全部构建函数为纯函数：对全默认（从未 set）消息可读、不抛，键位稳定帧结构。


def _round_list(values, nd: int = 3) -> list[float]:
  return [round(float(v), nd) for v in values]


def _first(values, nd: int = 3) -> float:
  vals = list(values)
  return round(float(vals[0]), nd) if vals else 0.0


def _measurement(m, nd: int = 3) -> dict:
  """LiveLocationKalman.Measurement → {value, std, valid}。"""
  return {"value": _round_list(m.value, nd), "std": _round_list(m.std, nd), "valid": bool(m.valid)}


def device_snapshot(ds) -> dict:
  """deviceState → device 段（2Hz）：网络/存储/内存/温度/热状态/风扇。
  板温明细走 thermalZones + 各 TempC 字段（temperatureSensor 服务的 temp 段见
  temp_snapshot，二者互补）。"""
  return {
    "networkType": enum_name(log.DeviceState.NetworkType, ds.networkType.raw),
    "networkStrength": enum_name(log.DeviceState.NetworkStrength, ds.networkStrength.raw),
    "networkMetered": ds.networkMetered,
    "started": ds.started,
    "freeSpacePercent": round(float(ds.freeSpacePercent), 1),
    "memoryUsagePercent": int(ds.memoryUsagePercent),
    "gpuUsagePercent": int(ds.gpuUsagePercent),
    "cpuUsagePercent": _round_list(ds.cpuUsagePercent, 1),
    "powerDrawW": round(float(ds.powerDrawW), 2),
    "somPowerDrawW": round(float(ds.somPowerDrawW), 2),
    "cpuTempC": _round_list(ds.cpuTempC, 1),
    "gpuTempC": _round_list(ds.gpuTempC, 1),
    "memoryTempC": round(float(ds.memoryTempC), 1),
    "maxTempC": round(float(ds.maxTempC), 1),
    "thermalZones": [{"name": z.name, "tempC": round(float(z.temp), 1)} for z in ds.thermalZones],
    "thermalStatus": enum_name(log.DeviceState.ThermalStatus, ds.thermalStatus.raw),
    "fanSpeedPercentDesired": int(ds.fanSpeedPercentDesired),
  }


def peripheral_snapshot(ps) -> dict:
  """peripheralState → peripheral 段（2Hz）：panda 外设/电压电流/风扇转速。"""
  return {
    "pandaType": enum_name(log.PandaState.PandaType, ps.pandaType.raw),
    "voltage": int(ps.voltage),
    "current": int(ps.current),
    "fanSpeedRpm": int(ps.fanSpeedRpm),
  }


def manager_snapshot(ms) -> dict:
  """managerState → manager 段（2Hz）：各进程存活状态（running/shouldBeRunning/exitCode）。"""
  return {
    "processes": [
      {"name": p.name, "pid": int(p.pid), "running": p.running,
       "shouldBeRunning": p.shouldBeRunning, "exitCode": int(p.exitCode)}
      for p in ms.processes
    ],
  }


def proc_snapshot(pl) -> dict:
  """procLog → proc 段（0.5Hz）：内存总量与逐进程 CPU/内存（运维看板用，
  仅快照、不进推流，体积约 5-10KB）。cpuUser/cpuSystem 为进程累计占用百分比，
  rss/pss 单位 MB。"""
  mem = pl.mem
  return {
    "mem": {
      "totalMb": round(mem.total / 1048576, 1),
      "freeMb": round(mem.free / 1048576, 1),
      "availableMb": round(mem.available / 1048576, 1),
    },
    "procs": [
      {"name": p.name, "pid": int(p.pid),
       "cpuUser": round(float(p.cpuUser), 2), "cpuSystem": round(float(p.cpuSystem), 2),
       "threads": int(p.numThreads),
       "rssMb": round(p.memRss / 1048576, 1), "pssMb": round(p.memPss / 1048576, 1)}
      for p in pl.procs
    ],
  }


def backup_snapshot(bm) -> dict:
  """backupManagerSP → backup 段（1Hz，sunnypilot 专属）：备份/恢复状态与进度。"""
  cb = bm.currentBackup
  return {
    "backupStatus": enum_name(custom.BackupManagerSP.Status, bm.backupStatus.raw),
    "restoreStatus": enum_name(custom.BackupManagerSP.Status, bm.restoreStatus.raw),
    "backupProgress": round(float(bm.backupProgress), 3),
    "restoreProgress": round(float(bm.restoreProgress), 3),
    "lastError": str(bm.lastError or ""),
    "currentBackup": {
      "deviceId": str(cb.deviceId or ""),
      "version": int(cb.version),
      "isEncrypted": cb.isEncrypted,
      "createdAt": str(cb.createdAt or ""),
      "updatedAt": str(cb.updatedAt or ""),
    },
  }


def temp_snapshot(e) -> dict:
  """temperatureSensor → temp 段（2Hz）。SensorEventData 为匿名 union：temperature
  分支未被选中时 pycapnp 读取会抛，此处兜底为 None（客户端判读为无效）。"""
  try:
    temperature = round(float(e.temperature), 2)
  except Exception:
    temperature = None
  return {
    "timestamp": int(e.timestamp),
    "source": enum_name(log.SensorEventData.SensorSource, e.source.raw),
    "temperatureC": temperature,
  }


def car_params_snapshot(cp, cps) -> dict:
  """carParams + carParamsSP → carParams 段（0.02Hz，静态车辆身份/动力学参数）。
  SP 段合并进 "sp" 子对象（sunnypilot 专属 flags/安全参数）。"""
  return {
    "brand": str(cp.brand or ""),
    "carFingerprint": str(cp.carFingerprint or ""),
    "fuzzyFingerprint": cp.fuzzyFingerprint,
    "notCar": cp.notCar,
    "carVin": str(cp.carVin or ""),
    "passive": cp.passive,
    "dashcamOnly": cp.dashcamOnly,
    "pcmCruise": cp.pcmCruise,
    "flags": int(cp.flags),
    "minEnableSpeed": round(float(cp.minEnableSpeed), 2),
    "minSteerSpeed": round(float(cp.minSteerSpeed), 2),
    "maxLateralAccel": round(float(cp.maxLateralAccel), 2),
    "steerAtStandstill": cp.steerAtStandstill,
    "autoResumeSng": cp.autoResumeSng,
    "wheelbase": round(float(cp.wheelbase), 3),
    "centerToFront": round(float(cp.centerToFront), 3),
    "steerRatio": round(float(cp.steerRatio), 3),
    "steerLimitTimer": round(float(cp.steerLimitTimer), 2),
    "steerControlType": enum_name(car.CarParams.SteerControlType, cp.steerControlType.raw),
    "radarUnavailable": cp.radarUnavailable,
    "stopAccel": round(float(cp.stopAccel), 3),
    "steerActuatorDelay": round(float(cp.steerActuatorDelay), 3),
    "longitudinalActuatorDelay": round(float(cp.longitudinalActuatorDelay), 3),
    "openpilotLongitudinalControl": cp.openpilotLongitudinalControl,
    "transmissionType": enum_name(car.CarParams.TransmissionType, cp.transmissionType.raw),
    "radarDelay": round(float(cp.radarDelay), 3),
    "networkLocation": enum_name(car.CarParams.NetworkLocation, cp.networkLocation.raw),
    "sp": {
      "flags": int(cps.flags),
      "safetyParam": int(cps.safetyParam),
      "pcmCruiseSpeed": cps.pcmCruiseSpeed,
      "intelligentCruiseButtonManagementAvailable": cps.intelligentCruiseButtonManagementAvailable,
      "enableGasInterceptor": cps.enableGasInterceptor,
    },
  }


def controls_snapshot(cs) -> dict:
  """controlsState → controls 段（100Hz，快照取最新）：控制器输出与告警细节。
  与 selfdriveState 派生的 opstate/sound 互补：这里拿到底层 longControlState/
  curvature/forceDecel 与完整告警字段。本仓 ControlsState 的 state/alert 字段在
  deprecated group 下（carrot 树遗留布局）；vCruise 不重复暴露（car 段已有，且
  上游 m/s、kph 语义易混）。"""
  dep = cs.deprecated
  return {
    "state": enum_name(log.SelfdriveState.OpenpilotState, dep.state.raw),
    "enabled": dep.enabled,
    "active": dep.active,
    "alertText1": str(dep.alertText1 or ""),
    "alertText2": str(dep.alertText2 or ""),
    "alertType": str(dep.alertType or ""),
    "alertStatus": enum_name(log.SelfdriveState.AlertStatus, dep.alertStatus.raw),
    "alertSize": enum_name(log.SelfdriveState.AlertSize, dep.alertSize.raw),
    "alertSound": enum_name(log.SelfdriveState.AudibleAlert, dep.alertSound2.raw),
    "longControlState": enum_name(car.CarControl.Actuators.LongControlState, cs.longControlState.raw),
    "curvature": round(float(cs.curvature), 5),
    "desiredCurvature": round(float(cs.desiredCurvature), 5),
    "forceDecel": cs.forceDecel,
    "decelForTurn": dep.decelForTurn,
    "decelForModel": dep.decelForModel,
  }


def car_sp_snapshot(css) -> dict:
  """carStateSP → carSP 段（100Hz）：sunnypilot 扩展车辆状态。speedLimit 补齐
  本仓 stock carState 无此字段、car 段恒 0 的缺口（单位 m/s，原样直传）。"""
  return {"speedLimit": round(float(css.speedLimit), 2)}


def control_snapshot(cc, ccsp) -> dict:
  """carControl + carControlSP → control 段（100Hz，快照取最新）：op 实际下发的
  执行器指令（扭矩/转角/曲率/油刹）与 HUD 提示。mads 来自 carControlSP。"""
  a = cc.actuators
  hud = cc.hudControl
  cruise = cc.cruiseControl
  mads = ccsp.mads
  return {
    "enabled": cc.enabled,
    "latActive": cc.latActive,
    "longActive": cc.longActive,
    "leftBlinker": cc.leftBlinker,
    "rightBlinker": cc.rightBlinker,
    "currentCurvature": round(float(cc.currentCurvature), 5),
    "actuators": {
      "torque": round(float(a.torque), 4),
      "steeringAngleDeg": round(float(a.steeringAngleDeg), 2),
      "curvature": round(float(a.curvature), 5),
      "accel": round(float(a.accel), 3),
      "jerk": round(float(a.jerk), 3),
      "gas": round(float(a.gas), 3),
      "brake": round(float(a.brake), 3),
      "torqueOutputCan": round(float(a.torqueOutputCan), 2),
      "speed": round(float(a.speed), 2),
      "longControlState": enum_name(car.CarControl.Actuators.LongControlState, a.longControlState.raw),
    },
    "hud": {
      "setSpeed": round(float(hud.setSpeed), 2),
      "speedVisible": hud.speedVisible,
      "lanesVisible": hud.lanesVisible,
      "leadVisible": hud.leadVisible,
      "leftLaneVisible": hud.leftLaneVisible,
      "rightLaneVisible": hud.rightLaneVisible,
      "leftLaneDepart": hud.leftLaneDepart,
      "rightLaneDepart": hud.rightLaneDepart,
      "visualAlert": enum_name(car.CarControl.HUDControl.VisualAlert, hud.visualAlert.raw),
      "leadDistanceBars": int(hud.leadDistanceBars),
    },
    "cruise": {"cancel": cruise.cancel, "resume": cruise.resume, "override": cruise.override},
    "mads": {
      "enabled": mads.enabled,
      "active": mads.active,
      "available": mads.available,
      "state": enum_name(custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState, mads.state.raw),
    },
  }


def plan_snapshot(lp, lpsp, lmp) -> dict:
  """longitudinalPlan + longitudinalPlanSP + lateralManeuverPlan → plan 段（20Hz）。
  accels/speeds/jerks 为未来 2.5s 的规划序列；sp.scc 为智能巡航（弯道/地图限速），
  sp.speedLimit 为限速解析与辅助；desiredCurvature 取自 lateralManeuverPlan。"""
  scc = lpsp.smartCruiseControl
  sl = lpsp.speedLimit
  resolver = sl.resolver
  assist = sl.assist
  return {
    "hasLead": lp.hasLead,
    "fcw": lp.fcw,
    "source": enum_name(log.LongitudinalPlan.LongitudinalPlanSource, lp.longitudinalPlanSource.raw),
    "aTarget": round(float(lp.aTarget), 3),
    "shouldStop": lp.shouldStop,
    "allowThrottle": lp.allowThrottle,
    "allowBrake": lp.allowBrake,
    "accelBoost": round(float(lp.accelBoost), 3),
    "accels": _round_list(lp.accels),
    "speeds": _round_list(lp.speeds),
    "jerks": _round_list(lp.jerks),
    "processingDelay": round(float(lp.processingDelay), 3),
    "solverExecutionTime": round(float(lp.solverExecutionTime), 3),
    "desiredCurvature": round(float(lmp.desiredCurvature), 5),
    "sp": {
      "source": enum_name(custom.LongitudinalPlanSP.LongitudinalPlanSource, lpsp.longitudinalPlanSource.raw),
      "vTarget": round(float(lpsp.vTarget), 3),
      "aTarget": round(float(lpsp.aTarget), 3),
      "sccVision": {
        "state": enum_name(custom.LongitudinalPlanSP.SmartCruiseControl.VisionState, scc.vision.state.raw),
        "enabled": scc.vision.enabled,
        "active": scc.vision.active,
        "vTarget": round(float(scc.vision.vTarget), 3),
        "aTarget": round(float(scc.vision.aTarget), 3),
        "currentLateralAccel": round(float(scc.vision.currentLateralAccel), 3),
        "maxPredictedLateralAccel": round(float(scc.vision.maxPredictedLateralAccel), 3),
      },
      "sccMap": {
        "state": enum_name(custom.LongitudinalPlanSP.SmartCruiseControl.MapState, scc.map.state.raw),
        "enabled": scc.map.enabled,
        "active": scc.map.active,
        "vTarget": round(float(scc.map.vTarget), 3),
        "aTarget": round(float(scc.map.aTarget), 3),
      },
      "speedLimit": {
        "valid": resolver.speedLimitValid,
        "speed": round(float(resolver.speedLimit), 2),
        "dist": round(float(resolver.distToSpeedLimit), 1),
        "source": enum_name(custom.LongitudinalPlanSP.SpeedLimit.Source, resolver.source.raw),
        "offset": round(float(resolver.speedLimitOffset), 1),
        "final": round(float(resolver.speedLimitFinal), 2),
        "assistState": enum_name(custom.LongitudinalPlanSP.SpeedLimit.AssistState, assist.state.raw),
        "assistEnabled": assist.enabled,
        "assistActive": assist.active,
        "assistVTarget": round(float(assist.vTarget), 3),
        "assistATarget": round(float(assist.aTarget), 3),
      },
      "e2eAlerts": {"greenLight": lpsp.e2eAlerts.greenLightAlert, "leadDepart": lpsp.e2eAlerts.leadDepartAlert},
    },
  }


def loc_snapshot(llk) -> dict:
  """liveLocationKalman → loc 段（20Hz）：EKF 融合定位（车机 GNSS+IMU，质量远超
  客户端自产定位）。posGeodetic.value = [lat, lon, alt]（度/米）；bearing 由
  velNED 客户端自行推导。"""
  return {
    "status": enum_name(log.LiveLocationKalman.Status, llk.status.raw),
    "unixTimestampMillis": int(llk.unixTimestampMillis),
    "gpsWeek": int(llk.gpsWeek),
    "gpsTimeOfWeek": round(float(llk.gpsTimeOfWeek), 3),
    "inputsOK": llk.inputsOK,
    "posenetOK": llk.posenetOK,
    "gpsOK": llk.gpsOK,
    "sensorsOK": llk.sensorsOK,
    "deviceStable": llk.deviceStable,
    "excessiveResets": llk.excessiveResets,
    "timeSinceReset": round(float(llk.timeSinceReset), 3),
    "timeToFirstFix": round(float(llk.timeToFirstFix), 2),
    "posGeodetic": _measurement(llk.positionGeodetic, 7),
    "velNED": _measurement(llk.velocityNED, 3),
    "accelDevice": _measurement(llk.accelerationDevice, 3),
    "orientationNED": _measurement(llk.orientationNED, 4),
  }


def gps_snapshot(g) -> dict:
  """gpsLocationExternal → gps 段（10Hz）：原始 GNSS（与 loc 段互补，低精度兜底）。
  speed m/s、bearingDeg 度、vNED m/s。"""
  return {
    "latitude": round(float(g.latitude), 7),
    "longitude": round(float(g.longitude), 7),
    "altitude": round(float(g.altitude), 2),
    "speed": round(float(g.speed), 3),
    "bearingDeg": round(float(g.bearingDeg), 2),
    "horizontalAccuracy": round(float(g.horizontalAccuracy), 2),
    "verticalAccuracy": round(float(g.verticalAccuracy), 2),
    "bearingAccuracyDeg": round(float(g.bearingAccuracyDeg), 2),
    "speedAccuracy": round(float(g.speedAccuracy), 2),
    "unixTimestampMillis": int(g.unixTimestampMillis),
    "source": enum_name(log.GpsLocationData.SensorSource, g.source.raw),
    "vNED": _round_list(g.vNED),
    "hasFix": g.hasFix,
    "satelliteCount": int(g.satelliteCount),
  }


def map_snapshot(lmd) -> dict:
  """liveMapDataSP → map 段（1Hz，sunnypilot 专属）：在线地图限速/前方限速/路名。
  数据源是 op 侧 mapd 拉取的地图数据，非客户端上行，符合服务定位原则。"""
  return {
    "speedLimitValid": lmd.speedLimitValid,
    "speedLimit": round(float(lmd.speedLimit), 2),
    "speedLimitAheadValid": lmd.speedLimitAheadValid,
    "speedLimitAhead": round(float(lmd.speedLimitAhead), 2),
    "speedLimitAheadDistance": round(float(lmd.speedLimitAheadDistance), 1),
    "roadName": str(lmd.roadName or ""),
  }


def _lead_summary(l) -> dict:
  """LeadDataV3 取 t=0 首采样摘要（完整逐点序列体积大，不进快照）。"""
  return {
    "prob": round(float(l.prob), 3),
    "probTime": round(float(l.probTime), 2),
    "t": _first(l.t, 2),
    "x": _first(l.x, 2),
    "y": _first(l.y, 2),
    "v": _first(l.v, 2),
    "a": _first(l.a, 2),
  }


def model_snapshot(m, msp) -> dict:
  """modelV2 + modelDataV2SP → model 段（20Hz）：模型元信息与决策摘要。
  laneLines/leads 逐点序列不进快照（体积大且 frame.radar 已覆盖前车），
  leads 只留 t=0 首采样；SP 段合并进 "sp" 子对象。"""
  return {
    "frameId": int(m.frameId),
    "frameDropPerc": round(float(m.frameDropPerc), 1),
    "modelExecutionTime": round(float(m.modelExecutionTime), 3),
    "big": m.big,
    "confidence": enum_name(log.ModelDataV2.ConfidenceClass, m.confidence.raw),
    "engagedProb": round(float(m.meta.engagedProb), 3),
    "hardBrakePredicted": m.meta.hardBrakePredicted,
    "laneChangeState": enum_name(log.LaneChangeState, m.meta.laneChangeState.raw),
    "laneChangeDirection": enum_name(log.LaneChangeDirection, m.meta.laneChangeDirection.raw),
    "action": {
      "desiredCurvature": round(float(m.action.desiredCurvature), 5),
      "desiredAcceleration": round(float(m.action.desiredAcceleration), 3),
      "shouldStop": m.action.shouldStop,
    },
    "laneLineProbs": _round_list(m.laneLineProbs),
    "laneLineStds": _round_list(m.laneLineStds),
    "leads": [_lead_summary(l) for l in m.leadsV3],
    "sp": {
      "laneTurnDirection": enum_name(custom.ModelDataV2SP.TurnDirection, msp.laneTurnDirection.raw),
      "leftLaneChangeEdgeBlock": msp.leftLaneChangeEdgeBlock,
      "rightLaneChangeEdgeBlock": msp.rightLaneChangeEdgeBlock,
      "modelCurvature": round(float(msp.modelCurvature), 5),
      "blendfulCurvature": round(float(msp.blendfulCurvature), 5),
      "blendfulTrust": round(float(msp.blendfulTrust), 3),
      "blendfulGoingAround": msp.blendfulGoingAround,
      "blendfulLeftProb": round(float(msp.blendfulLeftProb), 3),
      "blendfulRightProb": round(float(msp.blendfulRightProb), 3),
      "blendfulLaneWidth": round(float(msp.blendfulLaneWidth), 3),
    },
  }


def dmodel_snapshot(dm) -> dict:
  """drivingModelData → dmodel 段（20Hz）：UI 侧驾驶模型数据。path 为多项式系数
  （非逐点序列，体积小），laneLineMeta 拿左右车道线 y 与置信度。"""
  return {
    "frameId": int(dm.frameId),
    "frameDropPerc": round(float(dm.frameDropPerc), 1),
    "modelExecutionTime": round(float(dm.modelExecutionTime), 3),
    "big": dm.big,
    "action": {
      "desiredCurvature": round(float(dm.action.desiredCurvature), 5),
      "desiredAcceleration": round(float(dm.action.desiredAcceleration), 3),
      "shouldStop": dm.action.shouldStop,
    },
    "laneLineMeta": {
      "leftY": round(float(dm.laneLineMeta.leftY), 2),
      "rightY": round(float(dm.laneLineMeta.rightY), 2),
      "leftProb": round(float(dm.laneLineMeta.leftProb), 3),
      "rightProb": round(float(dm.laneLineMeta.rightProb), 3),
    },
    "laneChangeState": enum_name(log.LaneChangeState, dm.meta.laneChangeState.raw),
    "laneChangeDirection": enum_name(log.LaneChangeDirection, dm.meta.laneChangeDirection.raw),
    "path": {
      "x": _round_list(dm.path.xCoefficients, 4),
      "y": _round_list(dm.path.yCoefficients, 4),
      "z": _round_list(dm.path.zCoefficients, 4),
    },
  }


def odom_snapshot(co) -> dict:
  """cameraOdometry → odom 段（20Hz）：视觉里程计（设备系速度/角速度 + 标准差）。"""
  return {
    "frameId": int(co.frameId),
    "trans": _round_list(co.trans),
    "rot": _round_list(co.rot),
    "transStd": _round_list(co.transStd),
    "rotStd": _round_list(co.rotStd),
    "wideFromDeviceEuler": _round_list(co.wideFromDeviceEuler),
    "wideFromDeviceEulerStd": _round_list(co.wideFromDeviceEulerStd),
  }


def calib_snapshot(ec) -> dict:
  """extrinsicsCalibration → calib 段（4Hz）：相机标定状态与外参。rpyCalib 为
  设备系标定欧拉角（roll/pitch/yaw），extrinsicMatrix 为 3x3 行优先矩阵。"""
  return {
    "status": enum_name(log.ExtrinsicsCalibration.Status, ec.calStatus.raw),
    "calPerc": int(ec.calPerc),
    "calCycle": int(ec.calCycle),
    "validBlocks": int(ec.validBlocks),
    "rpyCalib": _round_list(ec.rpyCalib, 4),
    "rpyCalibSpread": _round_list(ec.rpyCalibSpread, 4),
    "height": _round_list(ec.height, 3),
    "wideFromDeviceEuler": _round_list(ec.wideFromDeviceEuler, 4),
    "extrinsicMatrix": _round_list(ec.extrinsicMatrix, 5),
  }


def params_snapshot(ltp, ld, vp) -> dict:
  """lateralTorqueParameters + lateralDelay + vehicleParameters → params 段
  （4-20Hz）：横向扭矩参数标定、横向控制延迟估计、在线动力学参数估计。"""
  return {
    "latTorque": {
      "valid": ltp.valid,
      "useParams": ltp.useParams,
      "calPerc": int(ltp.calPerc),
      "latAccelFactorRaw": round(float(ltp.latAccelFactorRaw), 4),
      "latAccelOffsetRaw": round(float(ltp.latAccelOffsetRaw), 4),
      "frictionCoefficientRaw": round(float(ltp.frictionCoefficientRaw), 4),
      "latAccelFactorFiltered": round(float(ltp.latAccelFactorFiltered), 4),
      "latAccelOffsetFiltered": round(float(ltp.latAccelOffsetFiltered), 4),
      "frictionCoefficientFiltered": round(float(ltp.frictionCoefficientFiltered), 4),
    },
    "lateralDelay": {
      "lateralDelay": round(float(ld.lateralDelay), 4),
      "estimate": round(float(ld.lateralDelayEstimate), 4),
      "estimateStd": round(float(ld.lateralDelayEstimateStd), 4),
      "validBlocks": int(ld.validBlocks),
      "status": enum_name(log.LateralDelay.Status, ld.status.raw),
    },
    "vehicle": {
      "valid": vp.valid,
      "steerRatio": round(float(vp.steerRatio), 3),
      "steerRatioValid": vp.steerRatioValid,
      "stiffnessFactor": round(float(vp.stiffnessFactor), 3),
      "stiffnessFactorValid": vp.stiffnessFactorValid,
      "angleOffsetDeg": round(float(vp.angleOffsetDeg), 3),
      "angleOffsetValid": vp.angleOffsetValid,
      "angleOffsetAverageDeg": round(float(vp.angleOffsetAverageDeg), 3),
      "angleOffsetAverageValid": vp.angleOffsetAverageValid,
      "roll": round(float(vp.roll), 4),
    },
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

      # v5 快照段：只进 /state 与 WS snapshot（set_state），不进事件流。
      # 高频 topic（100Hz）快照取最新值，与 car/radar 段同模式（O(1) 字典写入）；
      # 多 topic 合成段（carParams/control/plan/model/params）任一来源更新即重建。
      if sm.updated["deviceState"]:
        self.set_state("device", device_snapshot(sm["deviceState"]))
      if sm.updated["peripheralState"]:
        self.set_state("peripheral", peripheral_snapshot(sm["peripheralState"]))
      if sm.updated["managerState"]:
        self.set_state("manager", manager_snapshot(sm["managerState"]))
      if sm.updated["procLog"]:
        self.set_state("proc", proc_snapshot(sm["procLog"]))
      if sm.updated["backupManagerSP"]:
        self.set_state("backup", backup_snapshot(sm["backupManagerSP"]))
      if sm.updated["temperatureSensor"]:
        self.set_state("temp", temp_snapshot(sm["temperatureSensor"]))
      if sm.updated["carParams"] or sm.updated["carParamsSP"]:
        self.set_state("carParams", car_params_snapshot(sm["carParams"], sm["carParamsSP"]))
      if sm.updated["controlsState"]:
        self.set_state("controls", controls_snapshot(sm["controlsState"]))
      if sm.updated["carStateSP"]:
        self.set_state("carSP", car_sp_snapshot(sm["carStateSP"]))
      if sm.updated["carControl"] or sm.updated["carControlSP"]:
        self.set_state("control", control_snapshot(sm["carControl"], sm["carControlSP"]))
      if sm.updated["longitudinalPlan"] or sm.updated["longitudinalPlanSP"] or sm.updated["lateralManeuverPlan"]:
        self.set_state("plan", plan_snapshot(sm["longitudinalPlan"], sm["longitudinalPlanSP"], sm["lateralManeuverPlan"]))
      if sm.updated["liveLocationKalman"]:
        self.set_state("loc", loc_snapshot(sm["liveLocationKalman"]))
      if sm.updated["gpsLocationExternal"]:
        self.set_state("gps", gps_snapshot(sm["gpsLocationExternal"]))
      if sm.updated["liveMapDataSP"]:
        self.set_state("map", map_snapshot(sm["liveMapDataSP"]))
      if sm.updated["modelV2"] or sm.updated["modelDataV2SP"]:
        self.set_state("model", model_snapshot(sm["modelV2"], sm["modelDataV2SP"]))
      if sm.updated["drivingModelData"]:
        self.set_state("dmodel", dmodel_snapshot(sm["drivingModelData"]))
      if sm.updated["cameraOdometry"]:
        self.set_state("odom", odom_snapshot(sm["cameraOdometry"]))
      if sm.updated["extrinsicsCalibration"]:
        self.set_state("calib", calib_snapshot(sm["extrinsicsCalibration"]))
      if sm.updated["lateralTorqueParameters"] or sm.updated["lateralDelay"] or sm.updated["vehicleParameters"]:
        self.set_state("params", params_snapshot(
          sm["lateralTorqueParameters"], sm["lateralDelay"], sm["vehicleParameters"]))

      payload = ticker.tick(now)
      if payload:
        self.emit("frame", payload)

      # 声音：与 soundd 主循环一致——每轮先过 main toggle 短路，再做推导。
      # update() 必须在拿到新 carState 后立刻调用，长按计时依赖逐帧喂同一批事件。
      toggled = cruise_main_toggle.update(sm["carState"].buttonEvents, sm["selfdriveState"].enabled)
      payload = tracker.update(sm, toggled)
      if payload:
        self.emit("sound", payload)

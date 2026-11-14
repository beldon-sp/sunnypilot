"""
mate — 车机事件实时推送服务（局域网）

把 poller.py 推导的边沿事件（events / opstate / button / sound）与 2Hz 周期状态帧
（frame：car/radar 段，喂驾驶屏连续值）通过 WebSocket 和 SSE 推给局域网客户端。

服务定位原则：mate 只推「op 能获取、而局域网客户端无法自产」的数据；数据源在
客户端侧的数据禁止回推——v2 的 navi kind（输入即 carrotmate 上行的导航数据）
因此移除。参考实现为 cp_byd local_collector，其中 admin 管理 API 与 10Hz 周期帧
均不移植（frame 为 2Hz 状态语义）。

接口：
  GET /ws                 WebSocket（heartbeat 20s）
  GET /stream             SSE（?kinds=events,sound 可过滤）
  GET /state                 一次性状态快照（car/radar/opstate/events/sound +
                             services/panda/meta，v5 起再扩 18 个纯快照段
                             device/…/params，见 poller.py v5 小节；不周期推流）
  GET /health             健康检查（不鉴权，含 poller 心跳新鲜度与 onroad 标志）
  GET /sounds/<file.wav>  播放 sound 事件对应的音效文件（随语言目录切换）
  POST /command           v6 动作命令（白名单 + offroad_only + 敏感鉴权；需确认命令回 pending）
  POST /command/confirm   v6 确认闭环回传（approve/deny，超时默认 deny）
  POST /route_geometry    v6 carrotmate 上行高分辨率路线几何（喂曲率预览，只降不升）

WS 客户端也可发 {"type":"snapshot"} 走同一条连接拿快照。协议文档见 mate/API.md。

协议 v6（当前，PROTOCOL_VERSION = 6，FLAVOR = "sunnypilot"）：kind 集
events/opstate/button/sound/frame/confirm 不变（v6 新增 confirm，见下）；
hello 带 fork 标识 flavor（客户端门控主键，非本 fork 的客户端按协议不匹配处理）；
frame 含 carrot 段（carrotMan 遥测，v4 引入）；/state 与 WS snapshot 扩展 19 个
纯快照段（device/peripheral/manager/proc/backup/temp/carParams/controls/carSP/
control/plan/loc/gps/map/model/dmodel/odom/calib/params，见 poller.py v5 小节与
API.md §5.3——文档计数写作 18，以段名列表为准）。旧客户端按既有约定忽略未知
段/未知字段即可，无破坏性变更。历史：v5 = params 读写 API（双向控制）；
v4 = frame 增加 carrot 段；v3 = kind 收窄 + 2Hz frame（v3 客户端收到版本不符的
error 后自行停止重连）。

v6 新增（本协议面均需 MateToken 已配置才能调用，见 require_sensitive_auth）：
- POST /command：动作命令通道（白名单命令，offroad_only；需确认的命令走
  服务端确认闭环，broadcast confirm kind 并等待 POST /command/confirm）。
- POST /command/confirm：确认闭环回传端点（approve/deny；超时默认 deny）。
- POST /route_geometry：carrotmate 上行高分辨率路线几何（喂 RouteCurvaturePreview，
  替代 256 点 polyline 的粗分辨率；只降不升，fail-open）。
- param 写白名单扩容：控车相关参数（ALC/MADS/personality/SCC 等）标注
  offroad_only，行驶中（或 onroad 状态未知时）一律拒写（409/503）。

WS 鉴权失败在升级后发 {"type":"error"} 帧再关闭（SSE 仍为 HTTP 401）。

可选鉴权：params 写 MateToken 后，/ws /stream /state /sounds 必须带 ?token=xxx 或
Authorization: Bearer xxx。白名单外的 key 走 /data/params/d 文件直读。

注册方式（system/manager/process_config.py）：
  PythonProcess("mate", "openpilot.mate.server", always_run, restart_if_crash=True)
"""
import asyncio
import datetime
import hmac
import json
import os
import threading
import time

from aiohttp import web, WSMsgType

from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params
from openpilot.common.realtime import set_core_affinity
from openpilot.common.swaglog import cloudlog
from openpilot.mate.poller import MatePoller

PORT = int(os.environ.get("MATE_PORT", "8083"))
PROTOCOL_VERSION = 6  # v6：新增 confirm kind、/command、/command/confirm、/route_geometry 与 offroad_only 写门控
FLAVOR = "sunnypilot"  # 上游 fork 标识：sunnypilot / carrotpilot，客户端据此区分数据面差异（如 carrotMan、SP 段的有无）
ALL_KINDS = ("events", "opstate", "button", "sound", "frame", "confirm")
STATE_KINDS = ("events", "opstate", "sound")  # emit 时同步写进 /state 快照的类型；
# frame 不在其中——其 car/radar 段由 poller 显式喂快照，kind 本身只广播不进快照
REPLAY_KINDS = ("events", "sound")  # 新连接先补发这两类的当前状态（frame 走快照初始化）
TOKEN_PARAM = "MateToken"
SLOW_CLIENT_QUEUE = 256
SSE_KEEPALIVE_S = 15.0
POLLER_STALE_S = 2.0  # poller 每轮（≤100ms）刷 meta，超这个秒数没刷即视为线程死亡

ASSETS_DIR = os.path.join(BASEDIR, "openpilot", "selfdrive", "assets")


def wallclock() -> float:
  """Unix 墙钟秒。time.time 被仓库 lint 禁用（monotonic 才是进程内计时），
  快照 ts 是跨机对时语义，必须墙钟；设备时钟错时客户端自行对齐。"""
  return datetime.datetime.now(datetime.UTC).timestamp()

_token_lock = threading.Lock()
_token_cache: tuple[float, str | None] = (0.0, None)


def read_token() -> str | None:
  """读取鉴权 token，1 秒缓存；Params 对白名单外 key 抛 UnknownKeyName，
  回落到直读 params 目录文件（本仓多个 daemon 同样做法）。"""
  global _token_cache
  with _token_lock:
    ts, cached = _token_cache
    if time.monotonic() - ts < 1.0:
      return cached
    val = None
    try:
      got = Params().get(TOKEN_PARAM)
      val = got.decode("utf-8", "ignore") if isinstance(got, (bytes, bytearray)) else str(got or "") or None
    except Exception:
      try:
        with open(os.path.join(Params().get_param_path(), TOKEN_PARAM), "rb") as f:
          val = f.read().decode("utf-8", "ignore") or None
      except OSError:
        val = None
    _token_cache = (time.monotonic(), val)
    return val


def resolve_wav_path(filename: str) -> str | None:
  """本仓 soundd 无多语言音效目录（与 soundd 加载逻辑一致，单 sounds 目录）"""
  path = os.path.join(ASSETS_DIR, "sounds", filename)
  return path if os.path.isfile(path) else None


def auth_ok(request: web.BaseRequest) -> bool:
  token = read_token()
  if not token:
    return True
  auth = request.headers.get("Authorization", "")
  provided = request.query.get("token") or (auth[7:] if auth.startswith("Bearer ") else "")
  return hmac.compare_digest(provided, token)


def check_auth(request: web.BaseRequest) -> None:
  """HTTP 语义的鉴权（SSE/state/sounds）：失败抛 401。WS 用 auth_ok 走 error 帧。"""
  if not auth_ok(request):
    raise web.HTTPUnauthorized(text='{"error":"invalid token"}', content_type="application/json")


# ── params 读写（#6 双向控制 + v6 白名单扩容）──────────────────────────
# 读：白名单内任意 key；写：白名单 + MateToken 鉴权 + offroad_only 门控。
# 响应形状对齐 CarrotPilot 7000 carrot_server 契约（{ok, values} / {ok, name, value}），
# carrotmate 端 CarrotParamClient 可按同一解析逻辑对接任一端口。
#
# 白名单条目：(kind, lo, hi, offroad_only)
# - kind：bool / int / float（None 表示无值域约束）
# - offroad_only=True 的键在 onroad（或 onroad 状态未知）时一律拒写——这些参数
#   直接改变控车行为，远程改写必须停车操作（controls-safety 门禁）。
#   v5 既有 8 键保持任意时刻可写（兼容既有客户端行为）。
PARAMS_WHITELIST: dict[str, tuple[str, float | None, float | None, bool]] = {
  # 导航变道闸门（carrotmate 上行，fail-open）
  "NaviGuidedLaneChangeEnabled": ("bool", None, None, False),
  "NaviLaneChangeDirectionEnabled": ("bool", None, None, False),  # 拨杆方向冲突抑制（默认关）
  "NaviLaneChangeTurnDistance": ("int", 0, 2000, False),        # meters
  "NaviLaneChangeCrossroadDistance": ("int", 0, 2000, False),   # meters
  "NaviLaneChangeSignalDistance": ("int", 0, 2000, False),      # meters, crossroad 缺席时红绿灯兜底
  "NaviLaneChangeDirectionDistance": ("int", 0, 2000, False),   # meters, 方向冲突窗口
  # 匝道/弯道曲率预减速（SCC-M 数据源仲裁，默认关）
  "CarrotRouteCurvatureEnabled": ("bool", None, None, False),
  # 兼容既有通道（cem 实验模式开关）
  "ExperimentalMode": ("bool", None, None, False),
  # ── v6 控车相关扩容（全部 offroad_only）──
  # 自动变道（selfdrive/controls/lib/auto_lane_change.py）
  "AutoLaneChangeTimer": ("int", -1, 5, True),        # -1=Off 0=Nudge 1=Nudgeless 2..5=0.5/1/2/3s
  "AutoLaneChangeBsmDelay": ("bool", None, None, True),
  "LaneTurnDesire": ("bool", None, None, True),       # 低速打灯变道
  "LaneTurnValue": ("float", 0, 100, True),           # 低速阈值（mph，默认 19）
  # MADS（sunnypilot）
  "Mads": ("bool", None, None, True),
  "MadsMainCruiseAllowed": ("bool", None, None, True),
  "MadsSteeringMode": ("int", 0, 2, True),
  "MadsUnifiedEngagementMode": ("bool", None, None, True),
  # 纵向 / 巡航
  "LongitudinalPersonality": ("int", 0, 2, True),     # 0=relaxed 1=standard 2=rapid
  "SpeedLimitMode": ("int", 0, 2, True),
  "SmartCruiseControlMap": ("bool", None, None, True),
  "SmartCruiseControlVision": ("bool", None, None, True),
  "IntelligentCruiseButtonManagement": ("bool", None, None, True),
}


def _param_readable(name: str) -> bool:
  return name in PARAMS_WHITELIST


def _param_check_offroad(name: str) -> web.Response | None:
  """offroad_only 键的写前门控：onroad 拒写（409），onroad 状态未知也拒写（503）。
  状态未知时宁可拒服务也不放行——「宁可迟钝不可错记」。返回 None 表示放行。"""
  if not PARAMS_WHITELIST[name][3]:
    return None
  onroad = hub.onroad()
  if onroad is True:
    return web.json_response({"ok": False, "error": f"{name} is offroad_only (driving)"}, status=409)
  if onroad is None:
    return web.json_response({"ok": False, "error": f"{name} is offroad_only but onroad state unknown"}, status=503)
  return None


def _param_coerce(name: str, value) -> object:
  """按白名单类型归一写入口的值；类型或值域不符抛 ValueError。

  BOOL 键宽容归一（真布尔 / 1|0 / "true"|"false"），兼容 carrotmate 现有
  value:1|0 的 carrot 7000 请求形态与 mate 的 value:true|false 形态。
  """
  kind, lo, hi, _ = PARAMS_WHITELIST[name]
  if kind == "bool":
    if isinstance(value, bool):
      return value
    if isinstance(value, int) and value in (0, 1):
      return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "1"):
      return True
    if isinstance(value, str) and value.strip().lower() in ("false", "0"):
      return False
    raise ValueError(f"{name} 需要 bool")
  if kind == "int":
    if isinstance(value, bool) or not isinstance(value, (int, float)):
      raise ValueError(f"{name} 需要 int")
    iv = int(value)
    if (lo is not None and iv < lo) or (hi is not None and iv > hi):
      raise ValueError(f"{name} 超出 [{lo}, {hi}]")
    return iv
  if kind == "float":
    if isinstance(value, bool) or not isinstance(value, (int, float)):
      raise ValueError(f"{name} 需要 float")
    fv = float(value)
    if (lo is not None and fv < lo) or (hi is not None and fv > hi):
      raise ValueError(f"{name} 超出 [{lo}, {hi}]")
    return fv
  raise ValueError(f"{name} 类型不支持写入")


def _param_out(name: str, raw) -> object:
  """读出口归一：BOOL 键回真布尔，INT 键回整数，FLOAT 键回浮点，异常路径回 None。"""
  kind = PARAMS_WHITELIST[name][0]
  try:
    if isinstance(raw, (bytes, bytearray)):
      raw = raw.decode("utf-8", "ignore").strip()
      raw = {"1": True, "0": False, "true": True, "false": False}.get(raw.lower(), raw) if kind == "bool" else raw
    if kind == "bool":
      return bool(raw)
    if kind == "int":
      return int(float(raw))
    if kind == "float":
      return float(raw)
  except (TypeError, ValueError):
    return None
  return raw


async def params_read_handler(request: web.Request) -> web.Response:
  names = [n for n in (request.query.get("names") or "").split(",") if n]
  if not names:
    return web.json_response({"ok": False, "error": "names required"}, status=400)
  unknown = [n for n in names if not _param_readable(n)]
  if unknown:
    return web.json_response({"ok": False, "error": f"keys not allowed: {','.join(unknown)}"}, status=403)
  params = Params()
  values = {}
  for name in names:
    try:
      raw = params.get(name)
    except Exception:
      raw = None
    values[name] = _param_out(name, raw)
  return web.json_response({"ok": True, "values": values})


async def param_set_handler(request: web.Request) -> web.Response:
  check_auth(request)
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "body must be json"}, status=400)
  name, value = body.get("name"), body.get("value")
  if not _param_readable(name):
    return web.json_response({"ok": False, "error": f"key not allowed: {name}"}, status=403)
  offroad_reject = _param_check_offroad(name)
  if offroad_reject is not None:
    return offroad_reject
  try:
    coerced = _param_coerce(name, value)
  except ValueError as e:
    return web.json_response({"ok": False, "error": str(e)}, status=400)
  try:
    Params().put(name, coerced, block=True)
  except Exception as e:
    return web.json_response({"ok": False, "error": f"param put failed: {e}"}, status=500)
  # 写后立即读回，回显设备侧实际值（客户端读回确认以此对账）
  try:
    echo = _param_out(name, Params().get(name))
  except Exception:
    echo = coerced
  return web.json_response({"ok": True, "name": name, "value": echo})


# ── v6 动作命令通道（/command + /command/confirm）──────────────────────
# 边界（controls-safety 门禁）：
# - 命令白名单严格收敛，只收编「仓库内有真实消费方」的机制，不发明伪造钩子
#   （manager 退出后 launch 脚本不重启 openpilot，故无 restart 命令——机制核实
#   前宁可没有）。
# - 全部命令 offroad_only；需确认的命令走服务端确认闭环：POST /command 先回
#   pending + broadcast confirm kind，客户端 POST /command/confirm 回传，超时
#   默认 deny。
# - 鉴权用 require_sensitive_auth：与 check_auth 同口径——MateToken 未配置即放行
#   （车内私有网络自用场景），已配置则强制比对（401）。
COMMANDS: dict[str, dict] = {
  # backup_manager（sunnypilot/sunnylink/backups/manager.py:253）轮询该 param 触发备份
  "trigger_backup": {"title": "创建参数备份", "confirm": False, "offroad_only": True},
  # 本地清理过期 swaglog / crash 日志（文件删除，必须二次确认）
  "clear_logs": {"title": "清理过期日志", "confirm": True, "offroad_only": True},
  # 设备重启：manager.py:183 主循环轮询 DoReboot → 退出 → HARDWARE.reboot()
  # （真钩子）。没有「只重启 openpilot 不重启设备」的机制——manager 退出后
  # launch 脚本只挂起不重启，故不提供 restart_openpilot 伪命令。
  "reboot_device": {"title": "重启设备", "confirm": True, "offroad_only": True},
}
CONFIRM_TIMEOUT_S = 60.0
LOG_SWAGLOG_KEEP_DAYS = 7
LOG_CRASH_KEEP_DAYS = 30

_confirm_lock = threading.Lock()
_pending_confirms: dict[str, dict] = {}  # id -> {command, deadline, ts, title}


def require_sensitive_auth(request: web.Request) -> None:
  """v6 敏感端点鉴权：与 check_auth 同口径——token 未配置即放行（车内私有
  网络自用，设备上不配 MateToken 即可全程免 token）；已配置则强制比对。
  想上锁的用户只需设置 MateToken，无需改代码。"""
  if not read_token():
    return
  if not auth_ok(request):
    raise web.HTTPUnauthorized(text='{"error":"invalid token"}', content_type="application/json")


def _command_check_offroad(name: str) -> web.Response | None:
  """命令的 offroad 门控：onroad 拒执行（409），状态未知拒执行（503）。"""
  if not COMMANDS[name]["offroad_only"]:
    return None
  onroad = hub.onroad()
  if onroad is True:
    return web.json_response({"ok": False, "error": f"command {name} is offroad_only (driving)"}, status=409)
  if onroad is None:
    return web.json_response({"ok": False, "error": f"command {name} is offroad_only but onroad state unknown"}, status=503)
  return None


def _exec_trigger_backup() -> str:
  """触发参数备份：backup_manager 仅在 offroad 轮询到该 param 时执行（真钩子）。"""
  Params().put("BackupManager_CreateBackup", True, block=True)
  return "BackupManager_CreateBackup set; backup_manager (offroad) will consume it"


def _exec_reboot_device() -> str:
  """设备重启：manager 主循环轮询到 DoReboot 后退出并 HARDWARE.reboot()（真钩子）。"""
  Params().put("DoReboot", True, block=True)
  return "DoReboot set; manager will exit and reboot the device"


def _exec_clear_logs(now: float | None = None) -> str:
  """删除过期日志：swaglog 超 7 天、crash 超 30 天。返回删除计数摘要。"""
  from openpilot.common.hardware.hw import Paths
  now = wallclock() if now is None else now
  removed = 0
  for root, keep_days in ((Paths.swaglog_root(), LOG_SWAGLOG_KEEP_DAYS), (Paths.crash_log_root(), LOG_CRASH_KEEP_DAYS)):
    try:
      entries = os.listdir(root)
    except OSError:
      continue
    for entry in entries:
      path = os.path.join(root, entry)
      try:
        if os.path.isfile(path) and now - os.path.getmtime(path) > keep_days * 86400:
          os.remove(path)
          removed += 1
      except OSError:
        continue
  return f"removed {removed} old log files (swaglog>{LOG_SWAGLOG_KEEP_DAYS}d, crash>{LOG_CRASH_KEEP_DAYS}d)"


_EXECUTORS = {
  "trigger_backup": _exec_trigger_backup,
  "clear_logs": _exec_clear_logs,
  "reboot_device": _exec_reboot_device,
}


def _create_confirm(command: str) -> dict:
  """登记一条待确认命令并 broadcast confirm 事件（所有默认订阅客户端可见）。"""
  import uuid
  meta = COMMANDS[command]
  confirm_id = uuid.uuid4().hex[:16]
  record = {
    "id": confirm_id,
    "command": command,
    "title": meta["title"],
    "ts": wallclock(),
    "deadline": time.monotonic() + CONFIRM_TIMEOUT_S,
  }
  with _confirm_lock:
    _pending_confirms[confirm_id] = record
  hub.emit("confirm", {
    "id": confirm_id, "command": command, "title": meta["title"],
    "timeout_s": CONFIRM_TIMEOUT_S, "status": "pending", "ts": record["ts"],
  })
  return {"id": confirm_id, "command": command, "title": meta["title"], "timeout_s": CONFIRM_TIMEOUT_S}


def _resolve_confirm(confirm_id: str, status: str) -> None:
  """确认终态（approved/denied/expired）broadcast，客户端据此关闭对应弹窗。"""
  hub.emit("confirm", {"id": confirm_id, "status": status, "ts": wallclock()})


def _pop_confirm(confirm_id: str) -> dict | None:
  with _confirm_lock:
    record = _pending_confirms.pop(confirm_id, None)
    if record is None:
      return None
    if time.monotonic() > record["deadline"]:
      return {"expired": True}
    return record


async def command_handler(request: web.Request) -> web.Response:
  require_sensitive_auth(request)
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "body must be json"}, status=400)
  name = body.get("command")
  if name not in COMMANDS:
    return web.json_response({"ok": False, "error": f"unknown command: {name}"}, status=400)
  offroad_reject = _command_check_offroad(name)
  if offroad_reject is not None:
    return offroad_reject
  if COMMANDS[name]["confirm"]:
    pending = _create_confirm(name)
    return web.json_response({"ok": True, "status": "pending", "confirm": pending})
  try:
    detail = await asyncio.to_thread(_EXECUTORS[name])
  except Exception as e:
    return web.json_response({"ok": False, "error": f"command failed: {e}"}, status=500)
  return web.json_response({"ok": True, "status": "executed", "command": name, "detail": detail})


async def command_confirm_handler(request: web.Request) -> web.Response:
  require_sensitive_auth(request)
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "body must be json"}, status=400)
  confirm_id, approve = body.get("id"), body.get("approve")
  if not confirm_id or not isinstance(approve, bool):
    return web.json_response({"ok": False, "error": "id and approve(bool) required"}, status=400)
  record = _pop_confirm(confirm_id)
  if record is None:
    return web.json_response({"ok": False, "error": "unknown confirm id"}, status=404)
  if record.get("expired"):
    _resolve_confirm(confirm_id, "expired")
    return web.json_response({"ok": False, "error": "confirm expired (default deny)"}, status=410)
  command = record["command"]
  if not approve:
    _resolve_confirm(confirm_id, "denied")
    return web.json_response({"ok": True, "status": "denied", "command": command})
  _resolve_confirm(confirm_id, "approved")
  try:
    detail = await asyncio.to_thread(_EXECUTORS[command])
  except Exception as e:
    return web.json_response({"ok": False, "error": f"command failed: {e}"}, status=500)
  return web.json_response({"ok": True, "status": "executed", "command": command, "detail": detail})


# ── v6 路线几何上行（/route_geometry）──────────────────────────────────
# carrotmate 用高德路线 API 拿到全程几何（远高于 7706 polyline 的 256 点降采样），
# 原子写本地 JSON 文件；plannerd 侧 RouteCurvaturePreview 按 mtime 新鲜度消费
# （见 selfdrive/carrot/route_curvature.py MateGeometrySource）。只降不升、fail-open。
GEOMETRY_MAX_POINTS = 1024
GEOMETRY_MIN_POINTS = 3
GEOMETRY_PATH_DEFAULT = "/data/mate/route_geometry.json"


def geometry_path() -> str:
  return os.environ.get("MATE_GEOMETRY_PATH", GEOMETRY_PATH_DEFAULT)


def _norm_geometry_points(points) -> list[dict]:
  """归一上行点集：[[lat,lon],...] 或 [{"latitude","longitude"},...]；非法点抛 ValueError。"""
  out = []
  for p in points:
    if isinstance(p, dict):
      lat, lon = p.get("latitude"), p.get("longitude")
    elif isinstance(p, (list, tuple)) and len(p) == 2:
      lat, lon = p[0], p[1]
    else:
      raise ValueError("point format must be [lat,lon] or {latitude,longitude}")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or isinstance(lat, bool) or isinstance(lon, bool):
      raise ValueError("point coordinates must be numbers")
    lat, lon = float(lat), float(lon)
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0) or lat != lat or lon != lon:
      raise ValueError(f"point out of range: {lat},{lon}")
    out.append({"latitude": lat, "longitude": lon})
  return out


async def route_geometry_handler(request: web.Request) -> web.Response:
  """POST /route_geometry {"points": [...], "sequence": 可选}。导航是行驶态活动，
  故不做 offroad 门控；限流靠点数上限与原子写。"""
  check_auth(request)
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "body must be json"}, status=400)
  points = body.get("points")
  if not isinstance(points, list):
    return web.json_response({"ok": False, "error": "points must be a list"}, status=400)
  if not (GEOMETRY_MIN_POINTS <= len(points) <= GEOMETRY_MAX_POINTS):
    return web.json_response({"ok": False, "error": f"points size must be {GEOMETRY_MIN_POINTS}..{GEOMETRY_MAX_POINTS}"}, status=400)
  try:
    norm = _norm_geometry_points(points)
  except ValueError as e:
    return web.json_response({"ok": False, "error": str(e)}, status=400)
  seq = body.get("sequence")
  seq = int(seq) if isinstance(seq, (int, float)) and not isinstance(seq, bool) else int(wallclock())
  path = geometry_path()
  payload = {"ts": wallclock(), "sequence": seq, "points": norm}
  try:
    await asyncio.to_thread(_atomic_write_json, path, payload)
  except OSError as e:
    return web.json_response({"ok": False, "error": f"write failed: {e}"}, status=500)
  return web.json_response({"ok": True, "points": len(norm), "sequence": seq})


def _atomic_write_json(path: str, payload: dict) -> None:
  """tmp + os.replace 原子写，消费侧永远不会读到半截 JSON。"""
  os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
  tmp = f"{path}.tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
  os.replace(tmp, path)


class Hub:
  """poller 线程 -> 事件循环 -> 各客户端队列的广播中心，并维护最新状态快照。"""

  def __init__(self):
    self.loop: asyncio.AbstractEventLoop | None = None
    self.clients: dict[asyncio.Queue, frozenset[str]] = {}
    self.replay: dict[str, str] = {}  # kind -> 最新 envelope（供新连接补发）
    self.snapshot: dict[str, dict] = {}  # kind -> 最新 data（GET /state 用）
    self._snapshot_lock = threading.Lock()
    self.start_time = time.monotonic()

  def add_client(self, kinds) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=SLOW_CLIENT_QUEUE)
    self.clients[q] = frozenset(kinds)
    return q

  def remove_client(self, q: asyncio.Queue) -> None:
    self.clients.pop(q, None)

  def set_state(self, kind: str, data: dict) -> None:
    """car/services/panda/meta 这类状态段只进快照，不广播、不补发。每段带自己的写入时间。"""
    with self._snapshot_lock:
      self.snapshot[kind] = {"ts": wallclock(), "data": data}

  def poller_age(self) -> float | None:
    """poller 心跳距今秒数；None=meta 从未写入（线程死在启动或根本没起）。"""
    with self._snapshot_lock:
      meta = self.snapshot.get("meta")
    if meta is None:
      return None
    return max(0.0, round(wallclock() - meta["ts"], 1))

  def onroad(self) -> bool | None:
    with self._snapshot_lock:
      meta = self.snapshot.get("meta")
    return None if meta is None else bool(meta["data"].get("onroad"))

  def emit(self, kind: str, data: dict) -> None:
    """任意线程可调。events/sound 同时维护补发快照；STATE_KINDS 进 /state。"""
    payload = json.dumps({"type": kind, "data": data}, ensure_ascii=False)
    if kind in STATE_KINDS:
      self.set_state(kind, data)
    if kind in REPLAY_KINDS:
      active = kind == "events" and data.get("active")
      playing = kind == "sound" and data.get("sound", "none") != "none"
      if active or playing:
        self.replay[kind] = payload
      else:
        self.replay.pop(kind, None)
    if self.loop is None:
      return
    self.loop.call_soon_threadsafe(self._broadcast, kind, payload)

  def _broadcast(self, kind: str, payload: str) -> None:
    for q, kinds in list(self.clients.items()):
      if kind not in kinds:
        continue
      try:
        if q.full():  # 慢客户端丢最旧（事件频率很低，正常不会触发）
          q.get_nowait()
        q.put_nowait((kind, payload))
      except (asyncio.QueueEmpty, RuntimeError):
        self.remove_client(q)

  def replay_snapshot(self) -> list[str]:
    return [self.replay[k] for k in REPLAY_KINDS if k in self.replay]

  def state_snapshot(self) -> dict:
    """当前全量状态快照；未收到过的段不出现（客户端据此判断数据新鲜度）"""
    with self._snapshot_lock:
      return {"ts": datetime.datetime.now(datetime.UTC).timestamp(), "data": dict(self.snapshot)}


hub = Hub()


async def ws_reject(ws: web.WebSocketResponse) -> None:
  """WS 鉴权失败：完成升级后发 error 帧再关闭——客户端（carrotmate 的 AuthFailed
  分支）按消息语义处理，而非笼统的连接失败。"""
  await ws.send_str(json.dumps({"type": "error", "data": {"msg": "invalid token"}}, ensure_ascii=False))
  await ws.close()


async def ws_handler(request: web.Request):
  ws = web.WebSocketResponse(heartbeat=20)
  await ws.prepare(request)
  if not auth_ok(request):
    await ws_reject(ws)
    return ws

  kinds = set(ALL_KINDS)
  q = hub.add_client(kinds)
  cloudlog.event("mate ws connected", n=len(hub.clients))
  await ws.send_str(json.dumps({
    "type": "hello",
    "data": {"service": "mate", "version": PROTOCOL_VERSION, "flavor": FLAVOR, "port": PORT, "kinds": list(ALL_KINDS)},
  }, ensure_ascii=False))
  for payload in hub.replay_snapshot():
    await ws.send_str(payload)

  async def pump():
    while True:
      _, payload = await q.get()
      await ws.send_str(payload)

  pump_task = asyncio.create_task(pump())
  try:
    async for msg in ws:
      if msg.type != WSMsgType.TEXT:
        continue
      try:
        req = json.loads(msg.data)
      except json.JSONDecodeError:
        continue
      rtype = req.get("type")
      if rtype == "subscribe":
        picked = {k for k in req.get("kinds", []) if k in ALL_KINDS}
        kinds.clear()
        kinds.update(picked or ALL_KINDS)
        await ws.send_str(json.dumps({"type": "subscribed", "data": {"kinds": sorted(kinds)}}))
      elif rtype == "snapshot":
        await ws.send_str(json.dumps({"type": "snapshot", "data": hub.state_snapshot()}, ensure_ascii=False))
      elif rtype == "ping":
        await ws.send_str(json.dumps({"type": "pong", "data": {"ts": time.monotonic()}}))
      # 未知消息忽略
  except (ConnectionResetError, RuntimeError):
    pass
  finally:
    pump_task.cancel()
    hub.remove_client(q)
    cloudlog.event("mate ws disconnected", n=len(hub.clients))
  return ws


async def sse_handler(request: web.Request):
  check_auth(request)
  picked = {k for k in request.query.get("kinds", "").split(",") if k in ALL_KINDS}
  q = hub.add_client(picked or ALL_KINDS)

  resp = web.StreamResponse(headers={
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
  })
  await resp.prepare(request)
  cloudlog.event("mate sse connected", n=len(hub.clients))
  try:
    await resp.write(b": connected\n\n")
    for payload in hub.replay_snapshot():
      kind = json.loads(payload)["type"]
      await resp.write(f"event: {kind}\ndata: {payload}\n\n".encode())
    while True:
      try:
        kind, payload = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_S)
      except TimeoutError:
        await resp.write(b": ping\n\n")
        continue
      await resp.write(f"event: {kind}\ndata: {payload}\n\n".encode())
  except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
    pass
  finally:
    hub.remove_client(q)
  return resp


async def sounds_handler(request: web.Request):
  """GET /sounds/<file>.wav：sound 事件的配套音效，目录逻辑与 soundd 一致"""
  check_auth(request)
  filename = request.match_info.get("filename", "")
  if filename != os.path.basename(filename) or not filename.endswith(".wav"):
    raise web.HTTPNotFound(text='{"error":"sound not found"}', content_type="application/json")
  path = await asyncio.to_thread(resolve_wav_path, filename)
  if path is None:
    raise web.HTTPNotFound(text='{"error":"sound not found"}', content_type="application/json")
  return web.FileResponse(path)


async def state_handler(request: web.Request):
  """GET /state：一次性全量快照（car/opstate/events/sound/panda 最新值 + 各自 ts，
  外加 services/meta 两段服务状态，每轮 poller 循环刷新）。
  未收到过的数据段不出现；data 全空时看 meta（poller 是否活着）与 services
  （哪个 topic 见过数据）定位原因。car 段在 offroad 后保持最后一次 onroad 状态并随 ts 变旧。"""
  check_auth(request)
  return web.json_response(hub.state_snapshot())


async def health_handler(request: web.Request):
  age = hub.poller_age()
  return web.json_response({
    "ok": age is not None and age < POLLER_STALE_S,
    "service": "mate",
    "uptime": round(time.monotonic() - hub.start_time, 1),
    "clients": len(hub.clients),
    "token_auth": read_token() is not None,
    "poller_age": age,
    "onroad": hub.onroad(),
  })


async def _bind_loop(_app: web.Application) -> None:
  hub.loop = asyncio.get_running_loop()


def main():
  try:
    set_core_affinity([0, 1, 2, 3])
  except Exception:
    cloudlog.exception("mate: failed to set core affinity")

  threading.Thread(target=MatePoller(hub.emit, hub.set_state).run, name="mate-poller", daemon=True).start()

  app = web.Application()
  app.on_startup.append(_bind_loop)
  app.add_routes([
    web.get("/ws", ws_handler),
    web.get("/stream", sse_handler),
    web.get("/state", state_handler),
    web.get("/health", health_handler),
    web.get("/sounds/{filename}", sounds_handler),
    web.get("/params", params_read_handler),
    web.post("/param_set", param_set_handler),
    web.post("/command", command_handler),
    web.post("/command/confirm", command_confirm_handler),
    web.post("/route_geometry", route_geometry_handler),
  ])
  cloudlog.event("mate listening", port=PORT)
  web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
  main()

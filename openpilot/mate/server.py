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

WS 客户端也可发 {"type":"snapshot"} 走同一条连接拿快照。协议文档见 mate/API.md。

协议 v5（当前，PROTOCOL_VERSION = 5，FLAVOR = "sunnypilot"）：kind 集
events/opstate/button/sound/frame 不变；hello 带 fork 标识 flavor（客户端门控主键，
非本 fork 的客户端按协议不匹配处理）；frame 含 carrot 段（carrotMan 遥测，v4 引入）；
/state 与 WS snapshot 扩展 19 个纯快照段（device/peripheral/manager/proc/backup/temp/
carParams/controls/carSP/control/plan/loc/gps/map/model/dmodel/odom/calib/params，
见 poller.py v5 小节与 API.md §5.3——文档计数写作 18，以段名列表为准）。旧客户端按
既有约定忽略未知段/未知字段即可，无破坏性变更。历史：v4 = frame 增加 carrot 段；
v3 = kind 收窄 + 2Hz frame（v3 客户端收到版本不符的 error 后自行停止重连）。

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
PROTOCOL_VERSION = 5  # v5：kind 集不变，/state 与 WS snapshot 扩展 18 个纯快照段（见 poller.py v5 小节）；旧客户端忽略未知段即可
FLAVOR = "sunnypilot"  # 上游 fork 标识：sunnypilot / carrotpilot，客户端据此区分数据面差异（如 carrotMan、SP 段的有无）
ALL_KINDS = ("events", "opstate", "button", "sound", "frame")
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


# ── params 读写（#6 双向控制：搭子调设备参数）──────────────────────────
# 读：白名单内任意 key；写：白名单 + MateToken 鉴权（设了 token 才验，同 WS/SSE 口径）。
# 响应形状对齐 CarrotPilot 7000 carrot_server 契约（{ok, values} / {ok, name, value}），
# carrotmate 端 CarrotParamClient 可按同一解析逻辑对接任一端口。
PARAMS_WHITELIST: dict[str, str] = {
  # 导航变道闸门（carrotmate 上行，fail-open）
  "NaviGuidedLaneChangeEnabled": "bool",
  "NaviLaneChangeDirectionEnabled": "bool",   # 拨杆方向冲突抑制（默认关）
  "NaviLaneChangeTurnDistance": "int",        # meters
  "NaviLaneChangeCrossroadDistance": "int",   # meters
  "NaviLaneChangeSignalDistance": "int",      # meters, crossroad 缺席时红绿灯兜底
  "NaviLaneChangeDirectionDistance": "int",   # meters, 方向冲突窗口
  # 匝道/弯道曲率预减速（SCC-M 数据源仲裁，默认关）
  "CarrotRouteCurvatureEnabled": "bool",
  # 兼容既有通道（cem 实验模式开关）
  "ExperimentalMode": "bool",
}
PARAMS_DISTANCE_RANGE = (0, 2000)  # meters，距离类参数统一界


def _param_readable(name: str) -> bool:
  return name in PARAMS_WHITELIST


def _param_coerce(name: str, value) -> object:
  """按白名单类型归一写入口的值；类型不符抛 ValueError。

  BOOL 键宽容归一（真布尔 / 1|0 / "true"|"false"），兼容 carrotmate 现有
  value:1|0 的 carrot 7000 请求形态与 mate 的 value:true|false 形态。
  """
  kind = PARAMS_WHITELIST[name]
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
    if iv < PARAMS_DISTANCE_RANGE[0] or iv > PARAMS_DISTANCE_RANGE[1]:
      raise ValueError(f"{name} 超出 {PARAMS_DISTANCE_RANGE}")
    return iv
  raise ValueError(f"{name} 类型不支持写入")


def _param_out(name: str, raw) -> object:
  """读出口归一：BOOL 键回真布尔，INT 键回整数，异常路径回 None。"""
  kind = PARAMS_WHITELIST[name]
  try:
    if kind == "bool":
      if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", "ignore").strip() == "1"
      return bool(raw)
    if kind == "int":
      return int(raw)
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
  ])
  cloudlog.event("mate listening", port=PORT)
  web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
  main()

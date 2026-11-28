"""mate 参数变更哈希链审计（v6.1，W3）。

移植自 mouxancnb/webui server/bridge/param_changes_service.py（其源为 carrot cp），
按 mate 规模裁剪：
- 保留 append-only JSONL + sha256 哈希链 + verify 重走校验 + 环形裁剪重链；
- 裁掉 fingerprint baseline 全套（mate 白名单仅 ~22 键，全量摘要对比价值低）；
- source 枚举对齐 mate 通道：param_api / command / device / unknown；
- 时间戳 wallclock 语义（int 墙钟秒，与 mate 既有 ts 口径一致；仓库 lint
  禁用 time.time，用 datetime UTC）。

为什么要有它：参数可从 mate API、命令执行、车机 UI / 拨轮按钮（设备侧漂移）
多条路写入，出问题时必须能回答「谁、什么时候、从什么改成什么」。审计日志
活在断电随机的车机上，截断/半行是正常损耗——哈希链断在哪，可信边界就在哪。

纪律：append 永不抛异常——审计失败不得拖垮参数写入本体。
"""
import datetime
import hashlib
import json
import os
import threading

# 覆盖一次长时间调参够用；车机磁盘不会被 GC，环形上限防无限增长
MAX_PARAM_CHANGE_RECORDS = 1000

PARAM_CHANGE_SOURCES = frozenset({"param_api", "command", "device", "unknown"})

GENESIS_HASH = "0" * 64

DEFAULT_STATE_DIR = "/data/mate"
STATE_DIR = os.environ.get("MATE_PARAM_CHANGES_DIR", DEFAULT_STATE_DIR)
PARAM_CHANGES_PATH = os.path.join(STATE_DIR, "param_changes.jsonl")

_write_lock = threading.Lock()

# 本进程已知的各参数最新值（漂移观察基线，device source 用）
_known_values: dict = {}
_known_lock = threading.Lock()


def changes_path() -> str:
  return PARAM_CHANGES_PATH


def _now() -> int:
  return int(datetime.datetime.now(datetime.UTC).timestamp())


def _canonical(payload: dict) -> str:
  """稳定序列化：同一记录永远哈希出同一结果（键序固定、无空格）。"""
  return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_hash(record: dict) -> str:
  """记录内容 + 前一条 hash 的联合哈希（链式）。"""
  body = {key: record.get(key) for key in ("ts", "name", "prev", "next", "source", "engaged")}
  body["prev_hash"] = record.get("prev_hash", GENESIS_HASH)
  return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def normalize_source(source) -> str:
  text = str(source or "").strip()
  return text if text in PARAM_CHANGE_SOURCES else "unknown"


def _read_lines() -> list[str]:
  try:
    with open(PARAM_CHANGES_PATH, "r", encoding="utf-8") as f:
      return [line for line in f.read().splitlines() if line.strip()]
  except OSError:
    return []


def _parse(line: str) -> dict | None:
  try:
    record = json.loads(line)
  except (json.JSONDecodeError, ValueError):
    return None
  return record if isinstance(record, dict) else None


def read_param_changes(limit: int = 0, name: str = "", source: str = "") -> list[dict]:
  """审计记录，新在前；可按 name / source 收窄。limit<=0 表示全部。"""
  records = [r for r in (_parse(line) for line in _read_lines()) if r is not None]
  if name:
    records = [r for r in records if str(r.get("name", "")) == str(name)]
  if source:
    records = [r for r in records if str(r.get("source", "")) == str(source)]
  records.reverse()
  if limit > 0:
    records = records[:limit]
  return records


def last_record() -> dict | None:
  for line in reversed(_read_lines()):
    record = _parse(line)
    if record is not None:
      return record
  return None


def append_param_change(name: str, prev, next_value, source: str = "param_api",
                        engaged: bool | None = None, ts: int | None = None) -> dict | None:
  """追加一条变更记录。永不抛异常：审计失败返回 None，不拖垮写入本体。"""
  key = str(name or "").strip()
  if not key:
    return None
  try:
    with _write_lock:
      previous = last_record()
      record = {
        "ts": int(ts if ts is not None else _now()),
        "name": key,
        "prev": prev,
        "next": next_value,
        "source": normalize_source(source),
        "engaged": bool(engaged) if engaged is not None else None,
        "prev_hash": str(previous.get("hash") or GENESIS_HASH) if previous else GENESIS_HASH,
      }
      record["hash"] = record_hash(record)
      os.makedirs(os.path.dirname(PARAM_CHANGES_PATH), exist_ok=True)
      with open(PARAM_CHANGES_PATH, "a", encoding="utf-8") as f:
        f.write(_canonical(record) + "\n")
      _trim_locked()
      note_known_value(key, next_value)
      return record
  except Exception:
    return None


def _trim_locked() -> None:
  """超过环形上限裁最旧记录；裁剪后整体重建哈希链，保证 verify 仍可通过。"""
  lines = _read_lines()
  if len(lines) <= MAX_PARAM_CHANGE_RECORDS:
    return
  records = [r for r in (_parse(line) for line in lines) if r is not None]
  kept = records[-MAX_PARAM_CHANGE_RECORDS:]
  prev_hash = GENESIS_HASH
  for record in kept:
    record["prev_hash"] = prev_hash
    record["hash"] = record_hash(record)
    prev_hash = record["hash"]
  tmp_path = PARAM_CHANGES_PATH + ".tmp"
  with open(tmp_path, "w", encoding="utf-8") as f:
    for record in kept:
      f.write(_canonical(record) + "\n")
  os.replace(tmp_path, PARAM_CHANGES_PATH)


def verify_param_changes() -> dict:
  """从链头重走全链，报第一处断点（截断 / 篡改 / 半行写坏都会在此定位）。"""
  lines = _read_lines()
  prev_hash = GENESIS_HASH
  checked = 0
  for index, line in enumerate(lines):
    record = _parse(line)
    if record is None:
      return _broken(index, checked, "record is not valid JSON")
    if str(record.get("prev_hash") or "") != prev_hash:
      return _broken(index, checked, "record does not link to the previous hash")
    if str(record.get("hash") or "") != record_hash(record):
      return _broken(index, checked, "record content does not match its hash")
    prev_hash = str(record.get("hash"))
    checked += 1
  return {"ok": True, "valid": True, "checked": checked, "broken_at": None, "reason": ""}


def _broken(index: int, checked: int, reason: str) -> dict:
  return {"ok": True, "valid": False, "checked": checked, "broken_at": index, "reason": reason}


# ── 设备侧漂移观察（source=device）────────────────────────────────────
# 车机 UI / 拨轮按钮直接写 Params，永远不经过 append_param_change——
# 靠消费方（poller 节流旁路）定期读白名单键、与本进程已知值比对抓漂移。


def note_known_value(name: str, value) -> None:
  with _known_lock:
    _known_values[str(name)] = value


def observe_param_values(values: dict, allowed: set | None = None) -> int:
  """记录不经 mate 的设备侧变更：值漂移即补 source=device 记录。
  首次见到的键只建基线不产生假记录。返回追加的记录数。"""
  if not isinstance(values, dict) or not values:
    return 0
  drifted = []
  with _known_lock:
    for name, value in values.items():
      key = str(name)
      if allowed is not None and key not in allowed:
        continue
      if key not in _known_values:
        _known_values[key] = value
        continue
      if _known_values[key] != value:
        drifted.append((key, _known_values[key], value))
        _known_values[key] = value
  for key, previous, current in drifted:
    append_param_change(key, previous, current, source="device")
  return len(drifted)


def known_values() -> dict:
  with _known_lock:
    return dict(_known_values)

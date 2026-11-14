"""carrotNavi route.polyline → 前瞻曲率限速（#4 匝道/弯道预减速）。

高德导航 polyline（WGS-84，全程 ≤256 个降采样点）按三点 Menger 曲率
估出各点弯道安全速度 v = sqrt(a_lat_max / κ)，供 SCC-M 作为目标速度
候选（只降不升）。适用场景是大曲率前瞻（高速弯、匝道入口减速提示）；
polyline 分辨率有限（长途约数百米一点），真实匝道内部曲率会被低估，
实时弯道控制仍由 vision SCC / model 负责 —— 本模块只是「提前松油门」
的粗粒度软顶，绝不提升目标速度。

设计约束（controls-safety 门禁）：
- fail-open：carrotNavi 掉线 / route 流无效时预览不可用，SCC-M 回落
  OSM MapTargetVelocities，行为与现状一致；
- 独立开关 CarrotRouteCurvatureEnabled（默认关），且 SCC-M 本身仍受
  SmartCruiseControlMap 开关约束；
- 不改纵向规划结构，只产出 SCC-M 已有的 v_target 候选。
"""
import datetime
import json
import math
import os
import time

R = 6373000.0  # approximate radius of earth in meters
TO_RADIANS = math.pi / 180

MIN_CURVATURE_V = 3.0  # m/s，曲率估出的速度下限（匝道也不低于步行级）
FLAT_CURVATURE = 1e-4  # 1/m，低于此视为直线，不参与减速

# carrotNavi 是 2 Hz 常规服务：新鲜度口径对齐 navi_lane_change_gate
CARROT_NAVI_TIMEOUT = 1.5

# mate server POST /route_geometry 落盘文件（v6）：carrotmate 用高德路线 API 的
# 全程几何（可达 1024 点），分辨率远高于 7706 polyline 的 256 点降采样，能覆盖
# 匝道内部曲率。新鲜度按文件 mtime 口径：推送端仅在路线变化/周期重算时写文件。
MATE_GEOMETRY_PATH = "/data/mate/route_geometry.json"
MATE_GEOMETRY_TIMEOUT = 30.0  # 秒，超时回落 carrotNavi polyline / OSM


def _wallclock() -> float:
  """与 mate/server.py wallclock 同口径的 Unix 秒（mtime 是墙钟语义）。"""
  return datetime.datetime.now(datetime.UTC).timestamp()


def mate_geometry_path() -> str:
  return os.environ.get("MATE_GEOMETRY_PATH", MATE_GEOMETRY_PATH)


def distance_to_point(ax, ay, bx, by) -> float:
  """haversine 距离，入参为弧度，输出米（与 map_controller 同实现）。"""
  a = math.sin((bx - ax) / 2) * math.sin((bx - ax) / 2) + \
      math.cos(ax) * math.cos(bx) * math.sin((by - ay) / 2) * math.sin((by - ay) / 2)
  c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
  return R * c


def _equirect_xy(lat_deg: float, lon_deg: float, lat0: float) -> tuple[float, float]:
  """等距圆柱投影到米制平面坐标（预览级精度足够）。"""
  x = R * lon_deg * TO_RADIANS * math.cos(lat0 * TO_RADIANS)
  y = R * lat_deg * TO_RADIANS
  return x, y


def _menger_curvature(ax, ay, bx, by, cx, cy) -> float:
  """三点 Menger 曲率（1/m）；共线/退化点返回 0。"""
  c_ab = math.hypot(bx - ax, by - ay)
  c_bc = math.hypot(cx - bx, cy - by)
  c_ca = math.hypot(ax - cx, ay - cy)
  # 有向面积 ×2（叉积）
  cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
  area = abs(cross) / 2.0
  denom = c_ab * c_bc * c_ca
  if denom < 1e-9 or area < 1e-9:
    return 0.0
  return 4.0 * area / denom


class MateGeometrySource:
  """读取 mate server /route_geometry 落盘的高分辨率路线几何（文件 IPC）。

  read() 按 mtime 增量：文件未变化直接回缓存（plannerd 20Hz 调用不能每帧读盘）；
  坏文件/非法点缓存为 None（同一 mtime 不重读），下一次推送自然恢复——fail-open。
  """

  def __init__(self, path: str | None = None):
    self._path = path or mate_geometry_path()
    self._last_mtime = -1.0
    self._last_points: list[dict] | None = None
    self._last_sequence = -1

  def read(self) -> list[dict] | None:
    try:
      mtime = os.stat(self._path).st_mtime
    except OSError:
      self._last_mtime, self._last_points, self._last_sequence = -1.0, None, -1
      return None
    if mtime != self._last_mtime:
      self._last_mtime = mtime
      self._last_points, self._last_sequence = self._load()
    return self._last_points

  def _load(self) -> tuple[list[dict] | None, int]:
    try:
      with open(self._path, encoding="utf-8") as f:
        payload = json.load(f)
      points = [{"latitude": float(p["latitude"]), "longitude": float(p["longitude"])} for p in payload["points"]]
      if len(points) < 3:
        raise ValueError("too few points")
      if any(abs(p["latitude"]) > 90.0 or abs(p["longitude"]) > 180.0 for p in points):
        raise ValueError("point out of range")
      return points, int(payload.get("sequence", -1))
    except (OSError, ValueError, KeyError, TypeError):
      return None, -1

  def fresh(self) -> bool:
    """文件 mtime 距今 < MATE_GEOMETRY_TIMEOUT 秒才算新鲜。"""
    try:
      mtime = os.stat(self._path).st_mtime
    except OSError:
      return False
    return (_wallclock() - mtime) < MATE_GEOMETRY_TIMEOUT

  def sequence(self) -> int:
    return self._last_sequence


class RouteCurvaturePreview:
  """从 carrotNavi.route.polyline 现算前瞻目标速度（按 route.sequence 缓存）。

  target_velocities 形状与 MapTargetVelocities 一致：
  [{'latitude': float, 'longitude': float, 'velocity': float(m/s)}, ...]，
  供 SmartCruiseControlMap 直接当候选点集消费。
  """

  def __init__(self, a_lat_max: float = 1.0, mate_source: MateGeometrySource | None = None):
    self.a_lat_max = a_lat_max
    self._mate_source = mate_source or MateGeometrySource()
    self._geo_sequence = -1
    self._geo_valid = False
    self._sequence = -1
    self._updated_mono = 0.0
    self.target_velocities: list[dict] = []
    self.valid = False

  def _reset(self):
    self._sequence = -1
    self._updated_mono = 0.0
    self.target_velocities = []
    self.valid = False
    self._invalidate_geometry()

  def _compute(self, points) -> list[dict]:
    """polyline（[{'latitude','longitude'}, ...]）→ 曲率限速点表。"""
    n = len(points)
    if n < 3:
      return []
    lat0 = sum(p['latitude'] for p in points) / n
    xy = [_equirect_xy(p['latitude'], p['longitude'], lat0) for p in points]

    # 每点曲率取邻域三点窗口的最大值（抗降采样噪声）
    curvatures = [0.0] * n
    for i in range(1, n - 1):
      k_prev = _menger_curvature(*xy[i - 2], *xy[i - 1], *xy[i]) if i >= 2 else 0.0
      k_mid = _menger_curvature(*xy[i - 1], *xy[i], *xy[i + 1])
      k_next = _menger_curvature(*xy[i], *xy[i + 1], *xy[i + 2]) if i <= n - 3 else 0.0
      curvatures[i] = max(k_prev, k_mid, k_next, 0.0)
    curvatures[0] = curvatures[1] if n > 1 else 0.0
    curvatures[n - 1] = curvatures[n - 2] if n > 1 else 0.0

    out = []
    for i, p in enumerate(points):
      k = curvatures[i]
      if k <= FLAT_CURVATURE:
        continue
      v_curve = max(math.sqrt(self.a_lat_max / k), MIN_CURVATURE_V)
      out.append({
        'latitude': p['latitude'],
        'longitude': p['longitude'],
        'velocity': v_curve,
      })
    return out

  def update(self, navi) -> None:
    """navi = sm['carrotNavi']（调用方保证新鲜），按 sequence 增量重算。"""
    route = navi.route
    if not route.meta.present or len(route.polyline) < 3:
      self._reset()
      return
    if route.sequence == self._sequence:
      self._updated_mono = time.monotonic()
      return

    points = [{'latitude': p.latitude, 'longitude': p.longitude} for p in route.polyline]
    self.target_velocities = self._compute(points)
    self._sequence = route.sequence
    self._updated_mono = time.monotonic()
    self.valid = len(self.target_velocities) > 0
    if not self.valid:
      self._reset()

  def mark_stale(self) -> None:
    """调用方检测到 carrotNavi 掉线时调用：预览失效，SCC-M 回落 OSM。"""
    self._reset()

  def _invalidate_geometry(self) -> None:
    self._geo_sequence = -1
    self._geo_valid = False

  def update_geometry(self) -> bool:
    """mate /route_geometry 高分辨率几何驱动重算。

    文件新鲜且点集有效时重算/沿用 target_velocities 并返回 True（SCC-M 优先消费）；
    文件过期/缺失/坏数据返回 False，调用方回落 carrotNavi polyline → OSM——fail-open。
    """
    if not self._mate_source.fresh():
      self._invalidate_geometry()
      return False
    points = self._mate_source.read()
    if points is None:
      self._invalidate_geometry()
      return False
    seq = self._mate_source.sequence()
    if seq != self._geo_sequence:
      self.target_velocities = self._compute(points)
      self._geo_sequence = seq
      self._geo_valid = len(self.target_velocities) > 0
      self._updated_mono = time.monotonic()
    return self._geo_valid

  def age(self) -> float:
    return time.monotonic() - self._updated_mono if self._updated_mono else float('inf')

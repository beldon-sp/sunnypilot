"""route_curvature 测试（#4 匝道/弯道预减速）。

- 直线 polyline → 无减速点
- 直角弯 → 弯道点速度 sqrt(a_lat/κ) 且受下限约束
- sequence 缓存：同 sequence 不重算；新 sequence 重算
- route 流无效 → 预览失效（SCC-M 回落 OSM，fail-open）
- v6 mate /route_geometry 高分辨率几何：mtime 新鲜度、坏文件 fail-open、
  update_geometry 仲裁（几何 > polyline）
"""
import json
import math
import os
import time
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.carrot.route_curvature import TO_RADIANS

from openpilot.selfdrive.carrot.route_curvature import (
  MateGeometrySource,
  MATE_GEOMETRY_TIMEOUT,
  RouteCurvaturePreview,
  _menger_curvature,
  distance_to_point,
)


def _polyline(points):
  return [{'latitude': la, 'longitude': lo} for la, lo in points]


def _route_msg(points, sequence=1):
  navi = SimpleNamespace(
    route=SimpleNamespace(
      meta=SimpleNamespace(present=True),
      sequence=sequence,
      polyline=[SimpleNamespace(latitude=la, longitude=lo) for la, lo in points],
    ),
  )
  return navi


# 一段 1km 直线沿赤道向东（经度步长 ≈ 100m）
STRAIGHT = [(0.0, lo) for lo in [i * 0.001 for i in range(10)]]

# 直角弯：沿经线向北 500m，再沿纬线向东 500m（点距 ≈ 100m）
CORNER = (
  [(i * 0.0009, 0.0) for i in range(5)] +          # 向北 5 点（点距 ≈100m）
  [(0.0036, i * 0.0009) for i in range(1, 5)]      # 向东 4 点
)


class TestCurvatureMath:
  def test_distance_to_point(self):
    # 赤道上 0.001 度 ≈ 111.3m（入参为弧度）
    d = distance_to_point(0.0, 0.0, 0.0, 0.001 * TO_RADIANS)
    assert 110 < d < 113

  def test_collinear_zero_curvature(self):
    k = _menger_curvature(0.0, 0.0, 1.0, 0.0, 2.0, 0.0)
    assert k == 0.0

  def test_right_angle_curvature(self):
    # 半径 1m 圆弧上三点 → 曲率 ≈ 1
    pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    k = _menger_curvature(*pts[0], *pts[1], *pts[2])
    # 外接圆半径 = √2/2 → κ = √2 ≈ 1.414
    assert 1.3 < k < 1.5


class TestPreview:
  def test_straight_line_no_points(self):
    preview = RouteCurvaturePreview()
    preview.update(_route_msg(STRAIGHT))
    assert preview.valid is False
    assert preview.target_velocities == []

  def test_corner_produces_slow_points(self):
    preview = RouteCurvaturePreview(a_lat_max=1.0)
    preview.update(_route_msg(CORNER))
    assert preview.valid is True
    vs = [p['velocity'] for p in preview.target_velocities]
    assert len(vs) > 0
    # 直角弯（半径 ~100m 量级）→ 弯道点速度明显低于直线巡航量级
    assert all(3.0 <= v < 30.0 for v in vs)
    assert min(vs) < 20.0

  def test_sequence_cache_no_recompute(self):
    preview = RouteCurvaturePreview()
    msg = _route_msg(CORNER, sequence=7)
    preview.update(msg)
    first = list(preview.target_velocities)
    # 同 sequence：缓存命中（内部不会变化），新鲜时间刷新
    old_mono = preview._updated_mono
    time.sleep(0.01)
    preview.update(msg)
    assert preview.target_velocities == first
    assert preview._updated_mono > old_mono

  def test_new_sequence_recomputes(self):
    preview = RouteCurvaturePreview()
    preview.update(_route_msg(CORNER, sequence=1))
    assert preview.valid is True
    preview.update(_route_msg(STRAIGHT, sequence=2))
    assert preview.valid is False  # 新路线全直线 → 无点、失效

  def test_invalid_route_resets(self):
    preview = RouteCurvaturePreview()
    preview.update(_route_msg(CORNER))
    assert preview.valid is True
    # route 流消失
    navi = SimpleNamespace(route=SimpleNamespace(meta=SimpleNamespace(present=False), sequence=1, polyline=[]))
    preview.update(navi)
    assert preview.valid is False
    assert preview.target_velocities == []

  def test_too_few_points_resets(self):
    preview = RouteCurvaturePreview()
    preview.update(_route_msg(CORNER[:2]))
    assert preview.valid is False

  def test_mark_stale(self):
    preview = RouteCurvaturePreview()
    preview.update(_route_msg(CORNER))
    preview.mark_stale()
    assert preview.valid is False
    assert preview.age() == float('inf')


class TestMateGeometrySource:
  """v6 mate /route_geometry 落盘文件的读取源（文件 IPC）。"""

  def _write(self, path, points, sequence=11, age_s=0.0):
    payload = {"ts": 0.0, "sequence": sequence, "points": [{"latitude": la, "longitude": lo} for la, lo in points]}
    with open(path, "w", encoding="utf-8") as f:
      json.dump(payload, f)
    if age_s:
      old = time.time() - age_s
      os.utime(path, (old, old))

  def test_read_valid(self, tmp_path):
    path = str(tmp_path / "geo.json")
    self._write(path, CORNER, sequence=42)
    src = MateGeometrySource(path)
    points = src.read()
    assert points is not None and len(points) == len(CORNER)
    assert src.sequence() == 42

  def test_read_cached_until_mtime_changes(self, tmp_path):
    path = str(tmp_path / "geo.json")
    self._write(path, CORNER)
    src = MateGeometrySource(path)
    first = src.read()
    assert src.read() is first  # mtime 未变：缓存命中，不重读盘

  def test_bad_file_fail_open(self, tmp_path):
    for content in ("not json", '{"sequence": 1, "points": [{"latitude": 1.0}]}', '{"points": []}'):
      path = str(tmp_path / "geo.json")
      with open(path, "w", encoding="utf-8") as f:
        f.write(content)
      src = MateGeometrySource(path)
      assert src.read() is None

  def test_out_of_range_rejected(self, tmp_path):
    path = str(tmp_path / "geo.json")
    self._write(path, [(95.0, 116.0), (95.1, 116.1), (95.2, 116.2)])
    src = MateGeometrySource(path)
    assert src.read() is None

  def test_missing_file(self, tmp_path):
    src = MateGeometrySource(str(tmp_path / "nope.json"))
    assert src.read() is None
    assert src.fresh() is False

  def test_freshness_window(self, tmp_path):
    path = str(tmp_path / "geo.json")
    self._write(path, CORNER)
    src = MateGeometrySource(path)
    src.read()
    assert src.fresh() is True
    self._write(path, CORNER, age_s=MATE_GEOMETRY_TIMEOUT + 5)
    assert src.fresh() is False


class TestUpdateGeometry:
  """RouteCurvaturePreview.update_geometry：mate 几何优先、fail-open 回落。"""

  def test_geometry_drives_recompute(self, tmp_path):
    path = str(tmp_path / "geo.json")
    payload = {"ts": 0.0, "sequence": 5, "points": [{"latitude": la, "longitude": lo} for la, lo in CORNER]}
    with open(path, "w", encoding="utf-8") as f:
      json.dump(payload, f)
    preview = RouteCurvaturePreview(mate_source=MateGeometrySource(path))
    assert preview.update_geometry() is True
    assert preview.valid is False  # polyline 侧独立，未被几何污染
    assert len(preview.target_velocities) > 0

  def test_geometry_sequence_cache(self, tmp_path):
    path = str(tmp_path / "geo.json")
    payload = {"ts": 0.0, "sequence": 5, "points": [{"latitude": la, "longitude": lo} for la, lo in CORNER]}
    with open(path, "w", encoding="utf-8") as f:
      json.dump(payload, f)
    preview = RouteCurvaturePreview(mate_source=MateGeometrySource(path))
    preview.update_geometry()
    first = list(preview.target_velocities)
    preview.update_geometry()
    assert preview.target_velocities == first

  def test_stale_geometry_returns_false(self, tmp_path):
    path = str(tmp_path / "geo.json")
    payload = {"ts": 0.0, "sequence": 5, "points": [{"latitude": la, "longitude": lo} for la, lo in CORNER]}
    with open(path, "w", encoding="utf-8") as f:
      json.dump(payload, f)
    old = time.time() - MATE_GEOMETRY_TIMEOUT - 5
    os.utime(path, (old, old))
    preview = RouteCurvaturePreview(mate_source=MateGeometrySource(path))
    assert preview.update_geometry() is False

  def test_mark_stale_invalidates_geometry(self, tmp_path):
    path = str(tmp_path / "geo.json")
    payload = {"ts": 0.0, "sequence": 5, "points": [{"latitude": la, "longitude": lo} for la, lo in CORNER]}
    with open(path, "w", encoding="utf-8") as f:
      json.dump(payload, f)
    preview = RouteCurvaturePreview(mate_source=MateGeometrySource(path))
    assert preview.update_geometry() is True
    preview.mark_stale()
    # sequence 已失效：文件未变也会重算（回落后再次进入时语义正确）
    assert preview._geo_sequence == -1
    assert preview.update_geometry() is True


if __name__ == "__main__":
  pytest.main([__file__])

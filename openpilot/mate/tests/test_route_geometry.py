"""mate server v6 路线几何上行端点测试（#3 POST /route_geometry）。

- 合法点集原子写 JSON（tmp+replace），响应回点数与 sequence
- 点数界限（3..1024）、坐标范围、非法格式、非 list body → 400
- check_auth 语义与 /param_set 一致（配置了 token 必须带）

环境无 pytest-asyncio/anyio 插件，async 测试体经 asyncio.run() 同步包装。
"""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.mate import server


def _app() -> web.Application:
  app = web.Application()
  app.add_routes([web.post("/route_geometry", server.route_geometry_handler)])
  return app


@pytest.fixture
def geom_path(tmp_path, monkeypatch):
  path = str(tmp_path / "route_geometry.json")
  monkeypatch.setattr(server, "geometry_path", lambda: path)
  return path


def _run(coro):
  return asyncio.run(coro)


async def _client():
  c = TestClient(TestServer(_app()))
  await c.start_server()
  return c


def _route(n=50):
  return [[37.0 + i * 1e-4, 116.0 + i * 1e-4] for i in range(n)]


class TestRouteGeometryWrite:
  def test_valid_write(self, geom_path):
    async def body():
      client = await _client()
      try:
        resp = await client.post("/route_geometry", json={"points": _route(200), "sequence": 12345})
        assert resp.status == 200
        data = await resp.json()
        assert data == {"ok": True, "points": 200, "sequence": 12345}
        with open(geom_path, encoding="utf-8") as f:
          stored = json.load(f)
        assert stored["sequence"] == 12345
        assert len(stored["points"]) == 200
        assert stored["points"][0] == {"latitude": 37.0, "longitude": 116.0}
        assert "ts" in stored
      finally:
        await client.close()

    _run(body())

  def test_default_sequence_is_wallclock(self, geom_path):
    async def body():
      client = await _client()
      try:
        resp = await client.post("/route_geometry", json={"points": _route(10)})
        data = await resp.json()
        assert data["ok"] is True
        assert data["sequence"] > 1_700_000_000
      finally:
        await client.close()

    _run(body())

  def test_dict_point_form(self, geom_path):
    async def body():
      client = await _client()
      try:
        resp = await client.post("/route_geometry", json={"points": [{"latitude": 1.0, "longitude": 2.0}] * 5})
        assert resp.status == 200
      finally:
        await client.close()

    _run(body())


class TestRouteGeometryValidation:
  def test_size_bounds(self, geom_path):
    async def body():
      client = await _client()
      try:
        resp = await client.post("/route_geometry", json={"points": _route(2)})
        assert resp.status == 400
        resp = await client.post("/route_geometry", json={"points": _route(server.GEOMETRY_MAX_POINTS + 1)})
        assert resp.status == 400
        resp = await client.post("/route_geometry", json={"points": []})
        assert resp.status == 400
      finally:
        await client.close()

    _run(body())

  def test_bad_coords(self, geom_path):
    async def body():
      client = await _client()
      try:
        for points in (
          [[95.0, 116.0]] * 5,   # lat 越界
          [[37.0, 200.0]] * 5,   # lon 越界
          [["a", "b"]] * 5,      # 非数字
          [[True, 2.0]] * 5,     # bool 冒充数字
          [[37.0, 116.0, 3.0]] * 5,  # 点长度不为 2
        ):
          resp = await client.post("/route_geometry", json={"points": points})
          assert resp.status == 400, points[:1]
      finally:
        await client.close()

    _run(body())

  def test_body_must_have_points_list(self, geom_path):
    async def body():
      client = await _client()
      try:
        resp = await client.post("/route_geometry", json={"route": "x"})
        assert resp.status == 400
        resp = await client.post("/route_geometry", data=b"not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400
      finally:
        await client.close()

    _run(body())


class TestRouteGeometryAuth:
  def test_token_enforced_when_set(self, geom_path, monkeypatch):
    monkeypatch.setattr(server, "read_token", lambda: "secret")

    async def body():
      client = await _client()
      try:
        resp = await client.post("/route_geometry", json={"points": _route(10)})
        assert resp.status == 401
        resp = await client.post("/route_geometry", json={"points": _route(10)}, params={"token": "secret"})
        assert resp.status == 200
      finally:
        await client.close()

    _run(body())


if __name__ == "__main__":
  pytest.main([__file__])

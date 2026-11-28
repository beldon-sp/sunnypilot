# ruff: noqa: ASYNC230, ASYNC240 （测试内同步读写 /dev/shm 级小文件，无事件循环压力）
"""v6.2 UI 屏幕镜像服务端测试：WS mirror 订阅/二进制帧/.active 哨兵生命周期
+ GET /ui/mirror 快照 + 白名单 schema。UI 侧抓帧（GL）不在 CI 覆盖范围，
设备 offroad 验证补。"""
import asyncio
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

from openpilot.mate import server


def _run(coro):
  return asyncio.run(coro)


@pytest.fixture
def mirror_paths(tmp_path, monkeypatch):
  png = tmp_path / "mirror.png"
  active = tmp_path / "mirror.png.active"
  monkeypatch.setattr(server, "UI_MIRROR_PATH", str(png))
  monkeypatch.setattr(server, "UI_MIRROR_ACTIVE_PATH", str(active))
  monkeypatch.setattr(server, "UI_MIRROR_POLL_S", 0.05)
  return str(png), str(active)


@pytest.fixture
def tokened(monkeypatch):
  monkeypatch.setattr(server, "read_token", lambda: "secret")


@pytest.fixture
def fresh_hub(monkeypatch, tokened):
  # 真 Hub（非 stub）：add/remove consumer、广播全走真逻辑
  hub = server.Hub()
  monkeypatch.setattr(server, "hub", hub)
  return hub


def _app() -> "object":
  app = server.web.Application()
  app.add_routes([server.web.get("/ws", server.ws_handler),
                  server.web.get("/ui/mirror", server.ui_mirror_snapshot_handler),
                  server.web.get("/params/schema", server.params_schema_handler)])
  return app


class TestWsMirror:
  def test_enable_pushes_binary_and_sentinel_lifecycle(self, mirror_paths, fresh_hub):
    png_path, active_path = mirror_paths
    hub = fresh_hub

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      broadcaster = asyncio.create_task(server._mirror_broadcaster())
      try:
        ws = await client.ws_connect("/ws", params={"token": "secret"})
        hello = await ws.receive_json(timeout=3)
        assert hello["type"] == "hello"

        # enable → .active 哨兵出现
        await ws.send_json({"type": "mirror", "enable": True})
        state = await ws.receive_json(timeout=3)
        assert state == {"type": "mirrorState", "data": {"enabled": True}}
        assert os.path.exists(active_path)
        assert hub.mirror_consumers

        # UI 侧出帧（写 PNG + bump mtime）→ 广播二进制帧到达
        with open(png_path, "wb") as f:
          f.write(b"\x89PNG fake frame 1")
        os.utime(png_path, (1000, 1000))
        frame1 = await ws.receive_bytes(timeout=3)
        assert frame1 == b"\x89PNG fake frame 1"

        # 新帧才转发（mtime 变化）
        with open(png_path, "wb") as f:
          f.write(b"\x89PNG fake frame 2")
        os.utime(png_path, (1001, 1001))
        frame2 = await ws.receive_bytes(timeout=3)
        assert frame2 == b"\x89PNG fake frame 2"

        # disable → 哨兵消失、无消费者
        await ws.send_json({"type": "mirror", "enable": False})
        state = await ws.receive_json(timeout=3)
        assert state["data"] == {"enabled": False}
        assert not os.path.exists(active_path)
        assert not hub.mirror_consumers
        await ws.close()
      finally:
        broadcaster.cancel()
        await client.close()

    _run(body())

  def test_disconnect_cleans_sentinel(self, mirror_paths, fresh_hub):
    png_path, active_path = mirror_paths
    hub = fresh_hub

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        ws = await client.ws_connect("/ws", params={"token": "secret"})
        await ws.receive_json(timeout=3)
        await ws.send_json({"type": "mirror", "enable": True})
        await ws.receive_json(timeout=3)
        assert os.path.exists(active_path)
        # 直接断连 → finally 清理哨兵
        await ws.close()
        await asyncio.sleep(0.2)
        assert not hub.mirror_consumers
        assert not os.path.exists(active_path)
      finally:
        await client.close()

    _run(body())

  def test_slow_consumer_drops_not_blocks(self, mirror_paths, fresh_hub):
    png_path, _ = mirror_paths
    hub = fresh_hub

    async def body():
      q = asyncio.Queue(maxsize=1)
      hub.add_mirror_consumer(q)
      q.put_nowait(("full", b"stale"))  # 队列满
      hub.broadcast_mirror(b"new-frame")  # 不得抛异常、不得阻塞
      assert await q.get() == ("full", b"stale")  # 旧帧保留，新帧被丢

    _run(body())


class TestMirrorSnapshot:
  def test_503_when_not_active(self, mirror_paths, tokened):
    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.get("/ui/mirror", params={"token": "secret"})
        assert resp.status == 503
      finally:
        await client.close()

    _run(body())

  def test_200_png(self, mirror_paths, tokened):
    png_path, _ = mirror_paths
    with open(png_path, "wb") as f:
      f.write(b"\x89PNG data")

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.get("/ui/mirror", params={"token": "secret"})
        assert resp.status == 200
        assert resp.content_type == "image/png"
        assert await resp.read() == b"\x89PNG data"
        assert resp.headers["Cache-Control"] == "no-store"
        # 错 token 拒
        resp = await client.get("/ui/mirror", params={"token": "bad"})
        assert resp.status == 401
      finally:
        await client.close()

    _run(body())


class TestSchema:
  def test_ui_mirror_enabled_in_whitelist(self, mirror_paths, tokened):
    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.get("/params/schema", params={"token": "secret"})
        assert resp.status == 200
        schema = await resp.json()
        spec = schema["params"]["UiMirrorEnabled"]
        assert spec["kind"] == "bool" and spec["offroad_only"] is False
        assert spec["group"] == "misc"
      finally:
        await client.close()

    _run(body())

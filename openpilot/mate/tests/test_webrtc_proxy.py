"""mate WebRTC 信令代理测试（v6.1 V1）。

- /webrtc/schema：透传 webrtcd 可达性（不可达 503 / http 错误 502）
- /webrtc/offer：sdp 校验、camera 别名归一（不信任输入）、信令转发 mock
- 鉴权：require_sensitive_auth 口径（对齐 /command——行车画面隐私敏感面）

环境无 pytest-asyncio/anyio 插件，async 测试体经 asyncio.run() 同步包装。
"""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.mate import server


def _app() -> web.Application:
  app = web.Application()
  app.add_routes([
    web.get("/webrtc/schema", server.webrtc_schema_handler),
    web.post("/webrtc/offer", server.webrtc_offer_handler),
  ])
  return app


def _run(coro):
  return asyncio.run(coro)


class TestNormalizeCamera:
  def test_topic_aliases(self):
    assert server._normalize_camera("roadCameraState") == "road"
    assert server._normalize_camera("wideRoadCameraState") == "wideRoad"
    assert server._normalize_camera("driverCameraState") == "driver"
    assert server._normalize_camera("cabinCameraState") == "driver"
    assert server._normalize_camera("narrowRoadCameraState") == "road"

  def test_invalid_falls_back_to_road(self):
    for bad in ("", None, "hacker", 123, ["road"]):
      assert server._normalize_camera(bad) == "road"


class TestSchemaEndpoint:
  def test_reachable_passthrough(self, monkeypatch):
    async def fake_fetch():
      return 200, {"cameras": ["road", "wideRoad", "driver"]}
    monkeypatch.setattr(server, "_fetch_webrtcd_schema", fake_fetch)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.get("/webrtc/schema")
        data = await resp.json()
        assert data["ok"] is True and data["port"] == 5001
        assert data["schema"]["cameras"] == ["road", "wideRoad", "driver"]
      finally:
        await client.close()

    _run(body())

  def test_unreachable_503(self, monkeypatch):
    async def fake_fetch():
      raise ConnectionError("refused")
    monkeypatch.setattr(server, "_fetch_webrtcd_schema", fake_fetch)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.get("/webrtc/schema")
        assert resp.status == 503
        data = await resp.json()
        assert data["ok"] is False and "unreachable" in data["error"]
      finally:
        await client.close()

    _run(body())

  def test_http_error_502(self, monkeypatch):
    async def fake_fetch():
      return 500, None
    monkeypatch.setattr(server, "_fetch_webrtcd_schema", fake_fetch)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.get("/webrtc/schema")
        assert resp.status == 502
      finally:
        await client.close()

    _run(body())


class TestOfferEndpoint:
  def test_sdp_validation(self, monkeypatch):
    monkeypatch.setattr(server, "read_token", lambda: None)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.post("/webrtc/offer", json={"camera": "road"})
        assert resp.status == 400  # 缺 sdp
        resp = await client.post("/webrtc/offer", json={"sdp": "   "})
        assert resp.status == 400  # 空 sdp
        resp = await client.post("/webrtc/offer", data="not json")
        assert resp.status == 400
      finally:
        await client.close()

    _run(body())

  def test_offer_forwards_and_normalizes(self, monkeypatch):
    monkeypatch.setattr(server, "read_token", lambda: None)
    seen = []

    def fake_offer(sdp, camera):
      seen.append((sdp, camera))
      return {"type": "answer", "sdp": "v=0 fake-answer"}

    monkeypatch.setattr(server, "_post_webrtc_offer", fake_offer)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.post("/webrtc/offer", json={"sdp": "v=0 fake-offer", "camera": "driverCameraState"})
        data = await resp.json()
        assert data["ok"] is True and data["camera"] == "driver"  # 别名已归一
        assert data["type"] == "answer"
        assert seen == [("v=0 fake-offer", "driver")]
        # 非法 camera 回落 road，不透传
        resp = await client.post("/webrtc/offer", json={"sdp": "v=0", "camera": "evil"})
        assert (await resp.json())["camera"] == "road"
      finally:
        await client.close()

    _run(body())

  def test_offer_webrtcd_down_502(self, monkeypatch):
    monkeypatch.setattr(server, "read_token", lambda: None)

    def failing_offer(sdp, camera):
      raise TimeoutError("livestreaming service did not initialize in time.")

    monkeypatch.setattr(server, "_post_webrtc_offer", failing_offer)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.post("/webrtc/offer", json={"sdp": "v=0"})
        assert resp.status == 502
        assert "initialize" in (await resp.json())["error"]
      finally:
        await client.close()

    _run(body())

  def test_requires_sensitive_auth(self, monkeypatch):
    monkeypatch.setattr(server, "read_token", lambda: "secret")

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.get("/webrtc/schema")
        assert resp.status == 401  # 行车画面是隐私敏感面
        resp = await client.post("/webrtc/offer", json={"sdp": "v=0"})
        assert resp.status == 401
      finally:
        await client.close()

    _run(body())


if __name__ == "__main__":
  pytest.main([__file__])

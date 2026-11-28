"""mate UI 托管与鉴权中间件测试（v6.1 V2 + 决策点 1=A）。

- /ui/index.html：token 未配置可直进；已配置则中间件门禁（401）
- / 与 /ui：302 → /ui/index.html
- /command 系响应 X-Mate-Auth 提示头：none（裸奔）/ token（已上锁）
- 纯 API 模式：UI_DIR 逻辑仅覆盖 main()，此处测真实 web/ 目录

环境无 pytest-asyncio/anyio 插件，async 测试体经 asyncio.run() 同步包装。
"""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.mate import server


def _app() -> web.Application:
  app = web.Application(middlewares=[server.auth_middleware])
  app.add_routes([
    web.get("/", server.ui_index_handler),
    web.get("/ui", server.ui_index_handler),
    web.post("/command", server.command_handler),
  ])
  if server.UI_DIR:
    app.add_routes([web.static("/ui/", server.UI_DIR)])
  return app


def _run(coro):
  return asyncio.run(coro)


class TestUiHosting:
  def test_index_served_without_token(self, monkeypatch):
    monkeypatch.setattr(server, "read_token", lambda: None)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.get("/ui/index.html")
        assert resp.status == 200
        body = await resp.text()
        assert "mate" in body and "app.js" in body
      finally:
        await client.close()

    _run(body())

  def test_index_token_required_when_configured(self, monkeypatch):
    monkeypatch.setattr(server, "read_token", lambda: "secret")

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.get("/ui/index.html")
        assert resp.status == 401  # 中间件收口
        resp = await client.get("/ui/index.html", params={"token": "secret"})
        assert resp.status == 200
      finally:
        await client.close()

    _run(body())

  def test_root_and_ui_redirect(self, monkeypatch):
    monkeypatch.setattr(server, "read_token", lambda: None)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        for path in ("/", "/ui"):
          resp = await client.get(path, allow_redirects=False)
          assert resp.status == 302
          assert resp.headers["Location"] == "/ui/index.html"
      finally:
        await client.close()

    _run(body())


class TestXMatAuthHeader:
  def test_header_reflects_token_state(self, monkeypatch, tmp_path):
    # 裸奔状态：X-Mate-Auth: none
    monkeypatch.setattr(server, "read_token", lambda: None)
    p = __import__("openpilot.common.params", fromlist=["Params"]).Params(str(tmp_path))
    monkeypatch.setattr(server, "Params", lambda: p)

    class _StubHub:
      def onroad(self):
        return False

      def emit(self, kind, data):
        pass

    monkeypatch.setattr(server, "hub", _StubHub())

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.post("/command", json={"command": "no_such"})
        assert resp.headers["X-Mate-Auth"] == "none"
      finally:
        await client.close()

    _run(body())

    # 已配置 token：X-Mate-Auth: token（鉴权失败 401 走异常路径，无头——
    # 头只加在正常 JSON 响应上）
    monkeypatch.setattr(server, "read_token", lambda: "secret")

    async def body2():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.post("/command", json={"command": "no_such"}, params={"token": "secret"})
        assert resp.headers["X-Mate-Auth"] == "token"
      finally:
        await client.close()

    _run(body2())


if __name__ == "__main__":
  pytest.main([__file__])

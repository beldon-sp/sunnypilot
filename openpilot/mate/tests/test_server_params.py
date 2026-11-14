"""mate server params 读写端点测试（#6 双向控制）。

- 读：白名单内 key 回 BOOL→布尔 / INT→整数；白名单外 403
- 写：类型归一（bool/int）+ 距离参数界限（0..2000）；白名单外 403；
  MateToken 设了 token 必须验（401）
- 响应形状对齐 CarrotPilot 7000 carrot_server 契约（carrotmate 端同一解析）

环境无 pytest-asyncio/anyio 插件，async 测试体经 asyncio.run() 同步包装。
"""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.common.params import Params
from openpilot.mate import server


def _app() -> web.Application:
  app = web.Application()
  app.add_routes([
    web.get("/params", server.params_read_handler),
    web.post("/param_set", server.param_set_handler),
  ])
  return app


@pytest.fixture
def params_dir(tmp_path):
  p = Params(str(tmp_path))
  original = server.Params
  server.Params = lambda: p
  yield p
  server.Params = original


async def _client() -> TestClient:
  client = TestClient(TestServer(_app()))
  await client.start_server()
  return client


def _run(coro):
  return asyncio.run(coro)


class TestParamsRead:
  def test_read_bool_and_int(self, params_dir):
    params_dir.put("NaviGuidedLaneChangeEnabled", True, block=True)
    params_dir.put("NaviLaneChangeTurnDistance", 400, block=True)

    async def body():
      client = await _client()
      try:
        resp = await client.get("/params", params={"names": "NaviGuidedLaneChangeEnabled,NaviLaneChangeTurnDistance"})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["values"]["NaviGuidedLaneChangeEnabled"] is True
        assert data["values"]["NaviLaneChangeTurnDistance"] == 400
      finally:
        await client.close()

    _run(body())

  def test_read_missing_key_returns_default(self, params_dir):
    async def body():
      client = await _client()
      try:
        resp = await client.get("/params", params={"names": "NaviLaneChangeSignalDistance"})
        data = await resp.json()
        # 未写过 → 走 Params 默认值链（150）或 None，均为合法响应
        assert data["ok"] is True
        assert data["values"]["NaviLaneChangeSignalDistance"] in (150, None)
      finally:
        await client.close()

    _run(body())

  def test_read_whitelist_rejected(self, params_dir):
    async def body():
      client = await _client()
      try:
        resp = await client.get("/params", params={"names": "DongleId"})
        assert resp.status == 403
      finally:
        await client.close()

    _run(body())

  def test_read_missing_names_400(self, params_dir):
    async def body():
      client = await _client()
      try:
        resp = await client.get("/params")
        assert resp.status == 400
      finally:
        await client.close()

    _run(body())


class TestParamSet:
  def test_set_bool(self, params_dir):
    async def body():
      client = await _client()
      try:
        resp = await client.post("/param_set", json={"name": "NaviGuidedLaneChangeEnabled", "value": True})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["value"] is True
        assert params_dir.get_bool("NaviGuidedLaneChangeEnabled") is True
      finally:
        await client.close()

    _run(body())

  def test_set_int_clamps_range(self, params_dir):
    async def body():
      client = await _client()
      try:
        resp = await client.post("/param_set", json={"name": "NaviLaneChangeTurnDistance", "value": 5000})
        assert resp.status == 400
        resp = await client.post("/param_set", json={"name": "NaviLaneChangeTurnDistance", "value": 250})
        data = await resp.json()
        assert data["ok"] is True and data["value"] == 250
      finally:
        await client.close()

    _run(body())

  def test_set_type_mismatch_400(self, params_dir):
    async def body():
      client = await _client()
      try:
        resp = await client.post("/param_set", json={"name": "NaviGuidedLaneChangeEnabled", "value": 3})
        assert resp.status == 400
      finally:
        await client.close()

    _run(body())

  def test_set_whitelist_rejected(self, params_dir):
    async def body():
      client = await _client()
      try:
        resp = await client.post("/param_set", json={"name": "DongleId", "value": "x"})
        assert resp.status == 403
      finally:
        await client.close()

    _run(body())

  def test_set_requires_token_when_set(self, params_dir, monkeypatch):
    monkeypatch.setattr(server, "read_token", lambda: "secret")

    async def body():
      client = await _client()
      try:
        body_json = {"name": "CarrotRouteCurvatureEnabled", "value": True}
        resp = await client.post("/param_set", json=body_json)
        assert resp.status == 401
        resp = await client.post("/param_set", json=body_json, params={"token": "secret"})
        assert resp.status == 200
        assert (await resp.json())["value"] is True
      finally:
        await client.close()

    _run(body())


class TestWhitelistShape:
  def test_whitelist_covers_carrot_params(self):
    # 本批 1-7 号功能涉及的全部 param 都必须在白名单内（防止端到端断链）
    for key in ("NaviGuidedLaneChangeEnabled", "NaviLaneChangeDirectionEnabled",
                "NaviLaneChangeTurnDistance", "NaviLaneChangeCrossroadDistance",
                "NaviLaneChangeSignalDistance", "NaviLaneChangeDirectionDistance",
                "CarrotRouteCurvatureEnabled", "ExperimentalMode"):
      assert key in server.PARAMS_WHITELIST

  def test_no_other_params_leak(self):
    # 防扩散：白名单严格收敛，不得混入无关 key
    assert "MateToken" not in server.PARAMS_WHITELIST
    assert len(server.PARAMS_WHITELIST) == 8


if __name__ == "__main__":
  pytest.main([__file__])

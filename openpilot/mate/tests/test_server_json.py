"""mate JSON 出口 NaN/Infinity 消毒测试（v6.3.1）。

实车复现：GPS 未定位时 liveLocationKalman 的 posGeodetic std 全 NaN 进
loc 快照段，json.dumps 默认输出裸 NaN——浏览器 JSON.parse 抛，
/state 报「Cannot read properties of null (reading 'data')」。
"""
import asyncio
import json
import math

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.common.params import Params
from openpilot.mate import server


def _strict_loads(text):
  # 浏览器语义：裸 NaN/Infinity/-Infinity 一律视为非法 JSON
  def boom(const):
    raise ValueError(f"bare constant in JSON: {const}")
  return json.loads(text, parse_constant=boom)


class TestJsonDumps:
  def test_plain_payload_strict(self):
    assert _strict_loads(server._json_dumps({"a": 1, "b": "x"})) == {"a": 1, "b": "x"}

  def test_nan_inf_sanitized_to_none(self):
    obj = {"std": [float("nan"), math.inf, -math.inf], "nested": {"v": float("nan")}, "t": (float("nan"),)}
    out = _strict_loads(server._json_dumps(obj))
    assert out["std"] == [None, None, None]
    assert out["nested"]["v"] is None
    assert out["t"] == [None]

  def test_nonfinite_keys_survive(self):
    out = _strict_loads(server._json_dumps({"list": [{"deep": {"deeper": float("nan")}}]}))
    assert out["list"][0]["deep"]["deeper"] is None

  def test_unicode_kept_raw(self):
    assert server._json_dumps({"msg": "直播"}) == '{"msg": "直播"}'

  def test_tolerates_explicit_ensure_ascii(self):
    # 兼容调用点残留的 ensure_ascii kwarg（不重复传参不报错）
    assert server._json_dumps({"a": float("nan")}, ensure_ascii=False) == '{"a": null}'


class TestStateEndpoint:
  @pytest.fixture
  def params_dir(self, tmp_path, monkeypatch):
    p = Params(str(tmp_path))
    monkeypatch.setattr(server, "Params", lambda: p)
    yield p

  @pytest.fixture
  def clean_hub(self):
    snapshot, replay = dict(server.hub.snapshot), dict(server.hub.replay)
    server.hub.snapshot.clear()
    server.hub.replay.clear()
    yield
    server.hub.snapshot.clear()
    server.hub.snapshot.update(snapshot)
    server.hub.replay.clear()
    server.hub.replay.update(replay)

  def test_state_with_nan_loc_segment_is_strict_json(self, params_dir, clean_hub):
    server.hub.set_state("loc", {
      "posGeodetic": {"value": [36.37, -40.97, -2855.87], "std": [float("nan")] * 3, "valid": False},
    })
    app = web.Application()
    app.add_routes([web.get("/state", server.state_handler)])

    async def body():
      client = TestClient(TestServer(app))
      await client.start_server()
      try:
        resp = await client.get("/state")
        assert resp.status == 200
        return await resp.text()
      finally:
        await client.close()

    text = asyncio.run(body())
    parsed = _strict_loads(text)  # 裸 NaN 会在这里炸
    assert parsed["data"]["loc"]["data"]["posGeodetic"]["std"] == [None, None, None]
    assert parsed["data"]["loc"]["data"]["posGeodetic"]["valid"] is False

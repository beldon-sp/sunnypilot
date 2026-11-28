"""mate WebRTC 信令代理测试（v6.1 V1）。

- /webrtc/schema：透传 webrtcd 可达性（不可达 503 / http 错误 502）
- /webrtc/offer：sdp 校验、camera 别名归一（不信任输入）、信令转发 mock
- 鉴权：require_sensitive_auth 口径（对齐 /command——行车画面隐私敏感面）

环境无 pytest-asyncio/anyio 插件，async 测试体经 asyncio.run() 同步包装。
"""
import asyncio
import struct
from subprocess import TimeoutExpired

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


class TestLivestreamWakeup:
  """v6.3.1 自愈：webrtcd 被上游 teardown 关掉后，offer 置回参数并重试。"""

  def test_wakeup_sets_param_and_audits(self, monkeypatch, tmp_path):
    from openpilot.common.params import Params
    p = Params(str(tmp_path))
    p.put_bool("IsLiveStreaming", False)
    monkeypatch.setattr(server, "Params", lambda: p)
    audits = []
    monkeypatch.setattr(server.param_changes, "append_param_change",
                        lambda *a, **k: audits.append((a, k)))

    server._wakeup_livestream()
    assert p.get_bool("IsLiveStreaming") is True
    assert len(audits) == 1 and audits[0][0][0] == "IsLiveStreaming"

    # 已开时不重复写、不重复记审计
    server._wakeup_livestream()
    assert len(audits) == 1

  def test_offer_retries_after_wakeup(self, monkeypatch):
    monkeypatch.setattr(server, "read_token", lambda: None)
    from openpilot.system.webrtc import helpers
    calls = {"wait": 0}

    def flaky_wait(max_retries=10):
      calls["wait"] += 1
      if calls["wait"] == 1:
        raise TimeoutError("livestreaming service did not initialize in time.")

    def fake_post(body):
      return {"type": "answer", "sdp": "v=0 healed"}

    monkeypatch.setattr(helpers, "wait_for_webrtcd", flaky_wait)
    monkeypatch.setattr(helpers, "post_stream_request", fake_post)
    wakeups = []
    monkeypatch.setattr(server, "_wakeup_livestream", lambda: wakeups.append(1))

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.post("/webrtc/offer", json={"sdp": "v=0 fake"})
        data = await resp.json()
        assert resp.status == 200 and data["ok"] is True
        assert data["sdp"] == "v=0 healed"
        assert wakeups == [1]  # 首次不可达触发置位
        assert calls["wait"] == 2  # 置位后重连
      finally:
        await client.close()

    _run(body())

class TestMdnsRewrite:
  """v6.3.1：浏览器 offer 的 .local mDNS 候选转发前换成 IPv4（raw 查询，
  单播发向 offer 来源 + 组播兜底——iPhone 热点路由不桥接，组播到不了手机）。"""

  OFFER = (
    "v=0\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 99\r\n"
    "a=candidate:3660619743 1 udp 2113937151 aaa-bbb.local 59961 typ host generation 0\r\n"
    "a=candidate:1048927396 1 udp 2113939711 ccc-ddd.local 50960 typ host generation 0\r\n"
    "a=candidate:1048927396 1 udp 2113937151 aaa-bbb.local 62889 typ host\r\n"
  )

  def test_no_local_passthrough(self):
    sdp = "v=0\r\na=candidate:x 1 udp 1 172.20.10.3 54052 typ host\r\n"
    out, mapping, added = server._rewrite_mdns_candidates(sdp)
    assert out == sdp and mapping == {} and added == 0

  def test_resolved_names_replaced_everywhere(self, monkeypatch):
    answers = {"aaa-bbb.local": "172.20.10.1", "ccc-ddd.local": None}  # 后者解析失败
    monkeypatch.setattr(server, "_resolve_mdns", lambda n, p, **k: answers.get(n))
    out, mapping, added = server._rewrite_mdns_candidates(self.OFFER, "172.20.10.9")
    assert mapping == {"aaa-bbb.local": "172.20.10.1"}
    assert out.count("172.20.10.1") == 2  # 同名多 candidate 全部替换
    assert "ccc-ddd.local" in out        # 解析不到的保留原样
    assert added == 1                    # 但派生了 peer 兜底候选

  def test_peer_fallback_synthesizes_candidates(self, monkeypatch):
    monkeypatch.setattr(server, "_resolve_mdns", lambda n, p, **k: None)
    out, mapping, added = server._rewrite_mdns_candidates(self.OFFER, "172.20.10.1")
    assert mapping == {}
    assert added == 3  # OFFER 里 3 条 .local 候选逐条派生
    assert "a=candidate:matepeer 1 udp 2113937151 172.20.10.1 59961 typ host" in out
    assert "a=candidate:matepeer 1 udp 2113937151 172.20.10.1 62889 typ host" in out
    # 派生行紧跟原行之后，且不影响非 candidate 行
    lines = out.split("\r\n")
    for i, ln in enumerate(lines):
      if ln.startswith("a=candidate:matepeer"):
        assert lines[i - 1].startswith("a=candidate:") and ".local" in lines[i - 1]

  def test_resolve_timeout_kept_and_survives(self, monkeypatch):
    # _resolve_mdns 内部已吞 socket 异常返回 None；无 peer 时也不派生
    monkeypatch.setattr(server, "_resolve_mdns", lambda n, p, **k: None)
    out, mapping, added = server._rewrite_mdns_candidates(self.OFFER)
    assert mapping == {} and added == 0 and out == self.OFFER

  def test_query_build_and_answer_parse_roundtrip(self):
    name = "dead-beef.local"
    q = server._mdns_build_query(name)
    # QU 位必须置起（请求单播应答）
    assert q[-2:] == struct.pack(">H", 1 | 0x8000)
    # 合成一个 mDNSResponder 风格应答：question + 指针压缩的 A 记录
    enc = server._mdns_encode_name(name)
    question = enc + struct.pack(">HH", 1, 1 | 0x8000)
    rr = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 120, 4) + bytes([172, 20, 10, 1])
    resp = struct.pack(">6H", 0x4D23, 0x8400, 1, 1, 0, 0) + question + rr
    assert server._mdns_parse_answer(resp, name) == "172.20.10.1"
    # 自己查询的组播回环（QR=0）必须被忽略
    assert server._mdns_parse_answer(q + struct.pack(">HHHH", 0, 0, 0, 0), name) is None

  def test_parse_answer_ignores_other_record(self):
    name = "aa-bb.local"
    enc = server._mdns_encode_name(name)
    other = server._mdns_encode_name("zz-yy.local")
    question = enc + struct.pack(">HH", 1, 1)
    rr = other + struct.pack(">HHIH", 1, 1, 120, 4) + bytes([10, 0, 0, 9])  # 别的名字
    resp = struct.pack(">6H", 0, 0x8400, 1, 1, 0, 0) + question + rr
    assert server._mdns_parse_answer(resp, name) is None


  def test_wait_for_webrtcd_bounded_on_non_200(self, monkeypatch):
    # 回归：/schema 持续返回 500 时必须按 max_retries 收敛，不得死循环
    from openpilot.system.webrtc import helpers

    class Resp:
      ok = False

    monkeypatch.setattr(helpers.requests, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(helpers.time, "sleep", lambda s: None)
    try:
      helpers.wait_for_webrtcd(max_retries=3)
      raise AssertionError("should have raised TimeoutError")
    except TimeoutError:
      pass


if __name__ == "__main__":
  pytest.main([__file__])

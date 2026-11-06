import asyncio
import json

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.mate import server
from openpilot.mate.server import ALL_KINDS, PROTOCOL_VERSION, SLOW_CLIENT_QUEUE, Hub, ws_handler


class TestHubSnapshot:
  def test_set_state_stamped(self):
    hub = Hub()
    hub.set_state("car", {"vEgo": 10.0})
    snap = hub.state_snapshot()
    assert snap["data"]["car"] == {"ts": snap["data"]["car"]["ts"], "data": {"vEgo": 10.0}}
    assert snap["data"]["car"]["ts"] > 1_700_000_000  # unix 墙钟秒
    json.dumps(snap)

  def test_emit_updates_state_for_state_kinds_only(self):
    hub = Hub()  # loop 未绑定：emit 只落快照/补发，不广播
    hub.emit("opstate", {"state": "enabled", "enabled": True, "active": True, "engageable": True})
    hub.emit("button", {"button": "setCruise", "pressed": True})
    snap = hub.state_snapshot()
    assert snap["data"]["opstate"]["data"]["state"] == "enabled"
    assert "button" not in snap["data"]  # 边沿瞬时事件不进快照

  def test_frame_kind_and_v4_whitelist(self):
    # v3：navi 移除（回声——输入即客户端上行数据）；frame 为周期状态帧，
    # kind 本身不进快照也不补发（car/radar 段由 poller 显式喂快照）
    # v4：frame 增加 carrot 段（carrotMan 遥测），kind 白名单不变
    hub = Hub()
    hub.emit("frame", {"car": {"vEgo": 10.0}, "radar": {"leadOne": {"status": False}}})
    snap = hub.state_snapshot()
    assert "frame" not in snap["data"]
    assert hub.replay_snapshot() == []
    assert "navi" not in ALL_KINDS and "frame" in ALL_KINDS
    assert PROTOCOL_VERSION == 4

  def test_frame_reaches_only_subscribed_clients(self):
    hub = Hub()
    loop = asyncio.new_event_loop()
    hub.loop = loop
    try:
      q_all = hub.add_client(ALL_KINDS)
      q_other = hub.add_client(("events",))  # 未订 frame 的客户端收不到 frame
      hub.emit("frame", {"car": {"vEgo": 1.0}})
      hub.emit("events", {"added": [], "removed": [], "active": []})
      for _ in range(2):
        loop.run_until_complete(asyncio.sleep(0))
      assert q_other.get_nowait()[0] == "events"  # 只收到其订阅的 events
      assert q_other.empty()
      kinds = [q_all.get_nowait()[0] for _ in range(2)]
      assert kinds == ["frame", "events"]
    finally:
      loop.close()

  def test_slow_client_drops_oldest_last_wins(self):
    # frame 是状态语义：队列满丢最旧 ≈ last-wins，最新状态总能进队
    hub = Hub()
    loop = asyncio.new_event_loop()
    hub.loop = loop
    try:
      q = hub.add_client(ALL_KINDS)
      for i in range(SLOW_CLIENT_QUEUE):
        # 手动塞满队列（信封格式与 emit 输出一致）
        q.put_nowait(("frame", json.dumps({"type": "frame", "data": {"seq": i}})))
      hub.emit("frame", {"seq": SLOW_CLIENT_QUEUE})
      for _ in range(2):
        loop.run_until_complete(asyncio.sleep(0))
      assert q.qsize() == SLOW_CLIENT_QUEUE  # 不增长（不积压）
      seqs = [json.loads(q.get_nowait()[1])["data"]["seq"] for _ in range(SLOW_CLIENT_QUEUE)]
      assert seqs[0] == 1 and seqs[-1] == SLOW_CLIENT_QUEUE  # 最旧 seq=0 被丢
    finally:
      loop.close()

  def test_replay_follows_events_and_sound(self):
    hub = Hub()
    hub.emit("events", {"added": ["doorOpen"], "removed": [], "active": [{"name": "doorOpen"}]})
    assert len(hub.replay_snapshot()) == 1
    hub.emit("events", {"added": [], "removed": ["doorOpen"], "active": []})
    assert hub.replay_snapshot() == []  # 告警清空后不再补发
    assert hub.state_snapshot()["data"]["events"]["data"]["active"] == []  # 但快照保留空态

    hub.emit("sound", {"sound": "stopStop", "file": "audio_stopstop.wav", "loop": True,
                       "alertText1": "", "alertText2": ""})
    assert len(hub.replay_snapshot()) == 1
    hub.emit("sound", {"sound": "none", "file": None, "loop": False, "alertText1": "", "alertText2": ""})
    assert hub.replay_snapshot() == []


class TestHubServiceState:
  def test_poller_age_and_onroad(self):
    hub = Hub()
    assert hub.poller_age() is None and hub.onroad() is None  # meta 未写入 = poller 没跑
    hub.set_state("meta", {"frame": 7, "onroad": False})
    age = hub.poller_age()
    assert age is not None and age < 1.0
    assert hub.onroad() is False
    hub.set_state("meta", {"frame": 8, "onroad": True})
    assert hub.onroad() is True

  def test_status_sections_snapshot_only(self):
    hub = Hub()
    hub.set_state("services", {"carState": {"seen": False}})
    hub.set_state("panda", {"count": 0, "pandas": []})
    hub.set_state("radar", {"leadOne": {"status": False}})  # poller 显式喂的新段
    assert hub.replay_snapshot() == []  # 状态段只进 /state，不广播不补发
    snap = hub.state_snapshot()
    assert snap["data"]["services"]["data"]["carState"] == {"seen": False}
    assert snap["data"]["panda"]["data"] == {"count": 0, "pandas": []}
    assert snap["data"]["radar"]["data"]["leadOne"]["status"] is False


class TestWsAuth:
  def test_auth_failure_sends_error_frame_then_closes(self, monkeypatch):
    # v3：WS 鉴权失败不再抛 HTTP 401，而是完成升级 → error 帧 → 关闭
    # （carrotmate 的 AuthFailed 分支按消息语义处理）
    monkeypatch.setattr(server, "read_token", lambda: "secret")

    async def run():
      app = web.Application()
      app.add_routes([web.get("/ws", ws_handler)])
      client = TestClient(TestServer(app))
      await client.start_server()
      try:
        ws = await client.ws_connect("/ws?token=wrong")
        msg = json.loads((await ws.receive()).data)
        assert msg == {"type": "error", "data": {"msg": "invalid token"}}
        assert (await ws.receive()).type == WSMsgType.CLOSE
      finally:
        await client.close()

    loop = asyncio.new_event_loop()
    try:
      loop.run_until_complete(run())
    finally:
      loop.close()

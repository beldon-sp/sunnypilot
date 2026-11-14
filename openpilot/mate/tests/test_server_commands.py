"""mate server v6 命令通道与确认闭环测试（#2 /command + /command/confirm）。

- require_sensitive_auth：token 未配置 403（先配对），已配置校验比对（401/放行）
- 命令白名单严格收敛；offroad 门控（onroad 409 / 状态未知 503）
- 确认闭环：pending → confirm kind broadcast → approve 执行 / deny / 超时默认 deny
- clear_logs 执行器：只删过期文件，新文件保留

环境无 pytest-asyncio/anyio 插件，async 测试体经 asyncio.run() 同步包装。
"""
import asyncio
import os
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.mate import server


def _app() -> web.Application:
  app = web.Application()
  app.add_routes([
    web.post("/command", server.command_handler),
    web.post("/command/confirm", server.command_confirm_handler),
  ])
  return app


class _HubStub:
  """onroad 可控 + emit 收集（confirm broadcast 断言用）。"""

  def __init__(self, onroad):
    self._onroad = onroad
    self.emitted = []

  def onroad(self):
    return self._onroad

  def emit(self, kind, data):
    self.emitted.append((kind, data))


@pytest.fixture
def hub_offroad(monkeypatch):
  stub = _HubStub(onroad=False)
  monkeypatch.setattr(server, "hub", stub)
  return stub


@pytest.fixture
def tokened(monkeypatch):
  monkeypatch.setattr(server, "read_token", lambda: "secret")


def _run(coro):
  return asyncio.run(coro)


async def _post(client, path, json_body, token="secret"):
  return await client.post(path, json=json_body, params={"token": token} if token else None)


class TestCommandAuth:
  def test_token_unset_403(self, hub_offroad, monkeypatch):
    monkeypatch.setattr(server, "read_token", lambda: None)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await _post(client, "/command", {"command": "trigger_backup"}, token=None)
        assert resp.status == 403
        # 确认回传端点同样强制
        resp = await _post(client, "/command/confirm", {"id": "x", "approve": True}, token=None)
        assert resp.status == 403
      finally:
        await client.close()

    _run(body())

  def test_wrong_token_401(self, hub_offroad, tokened):
    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await _post(client, "/command", {"command": "trigger_backup"}, token="bad")
        assert resp.status == 401
      finally:
        await client.close()

    _run(body())


class TestCommandWhitelist:
  def test_unknown_command_400(self, hub_offroad, tokened):
    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await _post(client, "/command", {"command": "shutdown_device"})
        assert resp.status == 400
        # 无 restart_openpilot：manager 退出后 launch 脚本不重启，机制核实前不发明伪钩子
        resp = await _post(client, "/command", {"command": "restart_openpilot"})
        assert resp.status == 400
      finally:
        await client.close()

    _run(body())

  def test_registry_strict(self):
    assert set(server.COMMANDS) == {"trigger_backup", "clear_logs", "reboot_device"}
    # 全部命令 offroad_only；clear_logs 与 reboot_device 必须确认
    for name, meta in server.COMMANDS.items():
      assert meta["offroad_only"] is True
    assert server.COMMANDS["clear_logs"]["confirm"] is True
    assert server.COMMANDS["reboot_device"]["confirm"] is True
    assert server.COMMANDS["trigger_backup"]["confirm"] is False
    # 执行器注册完备
    assert set(server._EXECUTORS) == set(server.COMMANDS)


class TestOffroadGate:
  def test_onroad_409(self, tokened, monkeypatch):
    stub = _HubStub(onroad=True)
    monkeypatch.setattr(server, "hub", stub)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await _post(client, "/command", {"command": "trigger_backup"})
        assert resp.status == 409
      finally:
        await client.close()

    _run(body())

  def test_state_unknown_503(self, tokened, monkeypatch):
    stub = _HubStub(onroad=None)
    monkeypatch.setattr(server, "hub", stub)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await _post(client, "/command", {"command": "clear_logs"})
        assert resp.status == 503
      finally:
        await client.close()

    _run(body())


class TestTriggerBackup:
  def test_executes_and_sets_param(self, hub_offroad, tokened, monkeypatch, tmp_path):
    p = __import__("openpilot.common.params", fromlist=["Params"]).Params(str(tmp_path))
    monkeypatch.setattr(server, "Params", lambda: p)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await _post(client, "/command", {"command": "trigger_backup"})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True and data["status"] == "executed"
        assert p.get_bool("BackupManager_CreateBackup") is True
        assert hub_offroad.emitted == []  # 无需确认的命令不产生 confirm 事件
      finally:
        await client.close()

    _run(body())


class TestRebootDevice:
  def test_pending_then_confirm_sets_do_reboot(self, hub_offroad, tokened, monkeypatch, tmp_path):
    p = __import__("openpilot.common.params", fromlist=["Params"]).Params(str(tmp_path))
    monkeypatch.setattr(server, "Params", lambda: p)

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await _post(client, "/command", {"command": "reboot_device"})
        data = await resp.json()
        assert data["status"] == "pending"
        assert p.get_bool("DoReboot") is False  # 确认前不得执行（未写键回默认 False）
        resp = await _post(client, "/command/confirm", {"id": data["confirm"]["id"], "approve": True})
        data = await resp.json()
        assert data["status"] == "executed"
        assert p.get_bool("DoReboot") is True
      finally:
        await client.close()

    _run(body())


class TestConfirmFlow:
  """clear_logs：pending → confirm broadcast → approve 执行 / deny / 超时 deny。"""

  def test_pending_then_approve_executes(self, hub_offroad, tokened, monkeypatch):
    calls = []
    monkeypatch.setitem(server._EXECUTORS, "clear_logs", lambda: calls.append(1) or f"removed {len(calls)}")

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await _post(client, "/command", {"command": "clear_logs"})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "pending"
        confirm_id = data["confirm"]["id"]
        # pending 已 broadcast
        kinds = [k for k, _ in hub_offroad.emitted]
        assert kinds == ["confirm"]
        assert hub_offroad.emitted[0][1]["command"] == "clear_logs"

        resp = await _post(client, "/command/confirm", {"id": confirm_id, "approve": True})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "executed"
        assert calls == [1]
        # approved 终态已 broadcast
        assert hub_offroad.emitted[-1][1]["status"] == "approved"
        # 用完即焚：再次确认同一 id → 404
        resp = await _post(client, "/command/confirm", {"id": confirm_id, "approve": True})
        assert resp.status == 404
      finally:
        await client.close()

    _run(body())

  def test_deny_does_not_execute(self, hub_offroad, tokened, monkeypatch):
    calls = []
    monkeypatch.setitem(server._EXECUTORS, "clear_logs", lambda: calls.append(1) or "")

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await _post(client, "/command", {"command": "clear_logs"})
        confirm_id = (await resp.json())["confirm"]["id"]
        resp = await _post(client, "/command/confirm", {"id": confirm_id, "approve": False})
        data = await resp.json()
        assert data["status"] == "denied"
        assert calls == []
        assert hub_offroad.emitted[-1][1]["status"] == "denied"
      finally:
        await client.close()

    _run(body())

  def test_expired_default_deny(self, hub_offroad, tokened, monkeypatch):
    calls = []
    monkeypatch.setitem(server._EXECUTORS, "clear_logs", lambda: calls.append(1) or "")

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await _post(client, "/command", {"command": "clear_logs"})
        confirm_id = (await resp.json())["confirm"]["id"]
        # 手动把 deadline 拨到过去，模拟超时
        record = server._pending_confirms[confirm_id]
        record["deadline"] = time.monotonic() - 0.01
        resp = await _post(client, "/command/confirm", {"id": confirm_id, "approve": True})
        assert resp.status == 410
        assert calls == []
        assert hub_offroad.emitted[-1][1]["status"] == "expired"
      finally:
        await client.close()

    _run(body())

  def test_confirm_body_validation(self, hub_offroad, tokened):
    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await _post(client, "/command/confirm", {"id": "abc"})
        assert resp.status == 400  # 缺 approve
        resp = await _post(client, "/command/confirm", {"id": "abc", "approve": "yes"})
        assert resp.status == 400  # approve 非 bool
        resp = await _post(client, "/command/confirm", {"id": "nope", "approve": True})
        assert resp.status == 404
      finally:
        await client.close()

    _run(body())


class TestClearLogsExec:
  def test_only_old_files_removed(self, tmp_path, monkeypatch):
    swag = tmp_path / "swaglog"
    crash = tmp_path / "crash"
    swag.mkdir()
    crash.mkdir()

    old = swag / "swaglog.0001"
    new = swag / "swaglog.0002"
    old_crash = crash / "crash.0001"
    old.write_text("x")
    new.write_text("x")
    old_crash.write_text("x")
    ancient = time.time() - 40 * 86400
    recent = time.time() - 86400
    os.utime(old, (ancient, ancient))
    os.utime(new, (recent, recent))
    os.utime(old_crash, (ancient, ancient))

    class _StubPaths:
      @staticmethod
      def swaglog_root():
        return str(swag)

      @staticmethod
      def crash_log_root():
        return str(crash)

    import openpilot.common.hardware.hw as hw_mod
    monkeypatch.setattr(hw_mod, "Paths", _StubPaths)

    detail = server._exec_clear_logs()
    assert "removed 2" in detail
    assert not old.exists() and new.exists() and not old_crash.exists()


if __name__ == "__main__":
  pytest.main([__file__])

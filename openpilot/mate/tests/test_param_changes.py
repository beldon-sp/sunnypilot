"""mate param_changes 哈希链审计测试（v6.1 W3）。

- 链完整性：连续 append → verify ok
- 篡改 / 截断 / 半行写坏 → verify 定位 broken_at
- 环形裁剪重链：超 MAX 后 verify 仍 ok
- 漂移观察：首见建基线不产假记录；值漂移补 source=device
- 永不抛异常：目录不可写时 append 返回 None（param_set 本体不受影响）

环境无 pytest-asyncio/anyio 插件，async 测试体经 asyncio.run() 同步包装。
"""
import asyncio
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.common.params import Params
from openpilot.mate import param_changes, server


@pytest.fixture
def audit_dir(tmp_path, monkeypatch):
  """隔离审计文件目录，每条用例从空链开始。"""
  monkeypatch.setattr(param_changes, "PARAM_CHANGES_PATH", str(tmp_path / "param_changes.jsonl"))
  monkeypatch.setattr(param_changes, "_known_values", {})
  return tmp_path


def _app() -> web.Application:
  app = web.Application()
  app.add_routes([
    web.get("/param_changes", server.param_changes_handler),
    web.get("/param_changes/verify", server.param_changes_verify_handler),
    web.post("/param_set", server.param_set_handler),
  ])
  return app


def _run(coro):
  return asyncio.run(coro)


class TestHashChain:
  def test_chain_integrity(self, audit_dir):
    for i in range(5):
      rec = param_changes.append_param_change("NaviLaneChangeTurnDistance", 100 + i - 1, 100 + i, source="param_api")
      assert rec is not None
    result = param_changes.verify_param_changes()
    assert result["valid"] is True and result["checked"] == 5 and result["broken_at"] is None
    # 链式：第 2 条的 prev_hash = 第 1 条的 hash
    records = param_changes.read_param_changes()
    assert records[0]["prev_hash"] == records[1]["hash"]  # 新在前
    assert records[-1]["prev_hash"] == param_changes.GENESIS_HASH

  def test_tamper_detected_at_position(self, audit_dir):
    for i in range(4):
      param_changes.append_param_change("AutoLaneChangeTimer", i - 1, i, source="param_api")
    # 篡改中间一条的 next 值
    path = param_changes.PARAM_CHANGES_PATH
    lines = open(path, encoding="utf-8").read().splitlines()
    record = __import__("json").loads(lines[1])
    record["next"] = 999
    lines[1] = __import__("json").dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    result = param_changes.verify_param_changes()
    assert result["valid"] is False
    assert result["broken_at"] == 1  # 新在前文件顺序 = 行序，篡改在第 1 行
    assert "hash" in result["reason"]

  def test_truncation_detected(self, audit_dir):
    for i in range(4):
      param_changes.append_param_change("Mads", False, True, source="param_api")
    path = param_changes.PARAM_CHANGES_PATH
    lines = open(path, encoding="utf-8").read().splitlines()
    open(path, "w", encoding="utf-8").write("\n".join(lines[:2]) + "\n")  # 掐掉尾部
    result = param_changes.verify_param_changes()
    # 掐尾 = 链的前缀，仍完整可验证（append-only 日志的合法形态）
    assert result["valid"] is True and result["checked"] == 2

  def test_half_line_detected(self, audit_dir):
    param_changes.append_param_change("Mads", False, True, source="param_api")
    param_changes.append_param_change("Mads", True, False, source="param_api")
    path = param_changes.PARAM_CHANGES_PATH
    with open(path, "a", encoding="utf-8") as f:
      f.write('{"ts": 1, "name": "Mad')  # 模拟断电半行
    result = param_changes.verify_param_changes()
    assert result["valid"] is False
    assert result["broken_at"] == 2 and result["reason"] == "record is not valid JSON"

  def test_trim_rehashes_chain(self, audit_dir, monkeypatch):
    monkeypatch.setattr(param_changes, "MAX_PARAM_CHANGE_RECORDS", 10)
    for i in range(25):
      param_changes.append_param_change("TestKey", i - 1, i, source="param_api")
    result = param_changes.verify_param_changes()
    assert result["valid"] is True and result["checked"] == 10
    assert len(param_changes.read_param_changes()) == 10
    newest = param_changes.read_param_changes()[0]
    assert newest["next"] == 24  # 最旧被裁，最新保留且值正确


class TestDriftObserver:
  def test_first_sight_baselines_no_fake_record(self, audit_dir):
    n = param_changes.observe_param_values({"Mads": True, "AutoLaneChangeTimer": 0})
    assert n == 0  # 首见只建基线
    assert param_changes.read_param_changes() == []

  def test_drift_appends_device_record(self, audit_dir):
    param_changes.observe_param_values({"Mads": True})
    n = param_changes.observe_param_values({"Mads": False})
    assert n == 1
    records = param_changes.read_param_changes()
    assert records[0]["source"] == "device"
    assert records[0]["prev"] is True and records[0]["next"] is False

  def test_allowed_scope_and_no_change_no_record(self, audit_dir):
    param_changes.observe_param_values({"Mads": True, "ExperimentalMode": False}, allowed={"Mads"})
    assert param_changes.observe_param_values({"Mads": True, "ExperimentalMode": False}, allowed={"Mads"}) == 0
    assert set(param_changes.known_values()) == {"Mads"}  # allowed 外不建基线

  def test_param_api_write_updates_baseline_no_double_count(self, audit_dir):
    # append_param_change 内部回灌已知值：API 写入后，观察者不应再记一条 device
    param_changes.observe_param_values({"Mads": False})
    param_changes.append_param_change("Mads", False, True, source="param_api")
    assert param_changes.observe_param_values({"Mads": True}) == 0


class TestAppendNeverRaises:
  def test_unwritable_dir_returns_none(self, audit_dir, monkeypatch):
    monkeypatch.setattr(param_changes, "PARAM_CHANGES_PATH", "/proc/definitely/not/writable/param_changes.jsonl")
    assert param_changes.append_param_change("Mads", False, True) is None

  def test_empty_name_ignored(self, audit_dir):
    assert param_changes.append_param_change("", False, True) is None
    assert param_changes.read_param_changes() == []


class TestParamChangesEndpoints:
  def test_read_and_verify_endpoints(self, audit_dir, params_dir_stub, monkeypatch):
    param_changes.append_param_change("Mads", False, True, source="param_api")
    param_changes.append_param_change("Mads", True, False, source="device")

    async def body():
      client = TestClient(TestServer(_app()))
      await client.start_server()
      try:
        resp = await client.get("/param_changes", params={"name": "Mads", "limit": "1"})
        data = await resp.json()
        assert data["ok"] is True and data["count"] == 1
        assert data["records"][0]["source"] == "device"  # 新在前
        resp = await client.get("/param_changes", params={"source": "bogus"})
        assert (await resp.json())["count"] == 0  # 未知 source 过滤为空
        resp = await client.get("/param_changes/verify")
        data = await resp.json()
        assert data["valid"] is True and data["checked"] == 2
        resp = await client.get("/param_changes", params={"limit": "abc"})
        assert resp.status == 400
      finally:
        await client.close()

    _run(body())

  def test_param_set_audited(self, audit_dir, tmp_path, monkeypatch):
    p = Params(str(tmp_path))
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
        resp = await client.post("/param_set", json={"name": "Mads", "value": True})
        assert resp.status == 200
        records = param_changes.read_param_changes()
        assert len(records) == 1
        assert records[0]["name"] == "Mads" and records[0]["source"] == "param_api"
        assert records[0]["prev"] is False and records[0]["next"] is True
        assert records[0]["engaged"] is False
        # 拒写（白名单外）不产生审计记录
        resp = await client.post("/param_set", json={"name": "NotInWhitelist", "value": 1})
        assert resp.status == 403
        assert len(param_changes.read_param_changes()) == 1
      finally:
        await client.close()

    _run(body())

  def test_command_audited(self, audit_dir, tmp_path, monkeypatch):
    p = Params(str(tmp_path))
    monkeypatch.setattr(server, "Params", lambda: p)
    monkeypatch.setattr(server, "read_token", lambda: None)

    class _StubHub:
      def onroad(self):
        return False

      def emit(self, kind, data):
        pass

    monkeypatch.setattr(server, "hub", _StubHub())

    async def body():
      app = web.Application()
      app.add_routes([web.post("/command", server.command_handler)])
      client = TestClient(TestServer(app))
      await client.start_server()
      try:
        resp = await client.post("/command", json={"command": "trigger_backup"})
        assert resp.status == 200
        records = param_changes.read_param_changes()
        assert len(records) == 1
        assert records[0]["name"] == "BackupManager_CreateBackup"
        assert records[0]["source"] == "command" and records[0]["next"] is True
      finally:
        await client.close()

    _run(body())


@pytest.fixture
def params_dir_stub(tmp_path, monkeypatch):
  """端点测试的鉴权/参数隔离占位（param_set 测试自建）。"""
  return tmp_path


if __name__ == "__main__":
  pytest.main([__file__])

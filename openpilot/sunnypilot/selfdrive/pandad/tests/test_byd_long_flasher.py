#!/usr/bin/env python3
"""Regression tests for the BYD longitudinal firmware OTA flasher.

No hardware is touched: Panda, PandaDFU, HARDWARE, Params, messaging and
cloudlog are replaced with fakes. The real firmware and bootstub blobs are
read from the repo, so checksums, sector math and write chunking are exactly
what production uses.
"""
import hashlib
import math
import os

import pytest

from openpilot.sunnypilot.selfdrive.pandad import byd_long_flasher as m

FW = os.path.join(os.path.dirname(m.__file__), "byd_long_fw.bin.signed")
BOOTSTUB = os.path.join(os.path.dirname(m.__file__), "byd_long_bootstub.bin")


class FakeHandle:
  def __init__(self):
    self.control_writes = []
    self.bulk_writes = []

  def controlWrite(self, *args, **kwargs):
    self.control_writes.append(args)

  def bulkWrite(self, endpoint, data, **kwargs):
    self.bulk_writes.append((endpoint, data))
    return len(data)


class FakeLog:
  def __init__(self):
    self.records = []

  def __getattr__(self, name):
    def record(*args, **kwargs):
      self.records.append((name,) + args)
    return record


class FakeParams:
  store = {}

  def __init__(self, d=""):
    pass

  def get(self, key, *args, **kwargs):
    return FakeParams.store.get(key)


class FakeHardware:
  def __init__(self):
    self.recovers = 0

  def recover_internal_panda(self):
    self.recovers += 1


class FakeDFU:
  instances = []
  serials = ["dfu-serial-0001"]
  fail_list = False

  def __init__(self, serial):
    self.serial = serial
    self.programmed = None
    self.reset_called = False
    self.closed = False
    FakeDFU.instances.append(self)

  @classmethod
  def reset_class(cls):
    cls.instances = []
    cls.serials = ["dfu-serial-0001"]
    cls.fail_list = False

  @staticmethod
  def list():
    if FakeDFU.fail_list:
      return []
    return list(FakeDFU.serials)

  def program_bootstub(self, code):
    self.programmed = code

  def reset(self):
    self.reset_called = True

  def close(self):
    self.closed = True


class FakePanda:
  REQUEST_IN = 0xc0
  hw_type = b'\x05'  # UNO, F4
  enters_bootstub = True
  starts_in_bootstub = False
  connect_raises = False
  up_to_date = False
  instances = []
  sig_calls = 0
  expected_sig = b""

  def __init__(self, serial):
    self.serial = serial
    self._handle = FakeHandle()
    self.bootstub = FakePanda.starts_in_bootstub
    self.closed = False
    self.connects = 0
    FakePanda.instances.append(self)

  @classmethod
  def reset(cls):
    cls.instances = []
    cls.sig_calls = 0
    cls.hw_type = b'\x05'
    cls.enters_bootstub = True
    cls.starts_in_bootstub = False
    cls.connect_raises = False
    cls.up_to_date = False

  def get_type(self):
    return FakePanda.hw_type

  def get_signature(self):
    FakePanda.sig_calls += 1
    if FakePanda.up_to_date:
      return FakePanda.expected_sig
    # first read is the pre-flash firmware, later reads are the flashed one
    return FakePanda.expected_sig if FakePanda.sig_calls >= 2 else b'\x00' * 128

  def close(self):
    self.closed = True

  def reconnect(self):
    # reconnect() ends up in connect(wait=True), which has no exit condition
    raise AssertionError("reconnect() must never be used by the BYD flasher")

  def connect(self, claim=True, wait=False):
    assert wait is False, "wait=True loops forever when the board never comes back"
    if FakePanda.connect_raises:
      raise Exception("device went away")
    self.connects += 1
    # after the DFU bootstub write the board comes back in bootstub; after the
    # app flash it comes back as the running firmware
    self.bootstub = FakePanda.enters_bootstub and self.connects == 1

  @staticmethod
  def flasher_present(handle):
    return True

  @staticmethod
  def get_signature_from_firmware(fn):
    with open(fn, "rb") as f:
      return f.read()[-128:]

  @classmethod
  def usb_list(cls):
    # an internal SPI panda never appears here; filtering on it would skip the target
    raise AssertionError("usb_list() must not be used to filter panda serials")


def fake_log_from_bytes(data, schema):
  if data == b"BROKEN":
    raise ValueError("bad capnp")
  return type("CP", (), {"brand": "byd"})()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
  with open(FW, "rb") as f:
    FakePanda.expected_sig = f.read()[-128:]
  FakePanda.reset()
  FakeDFU.reset_class()
  FakeParams.store = {}
  log = FakeLog()
  hardware = FakeHardware()
  monkeypatch.setattr(m, "Panda", FakePanda)
  monkeypatch.setattr(m, "PandaDFU", FakeDFU)
  monkeypatch.setattr(m, "HARDWARE", hardware)
  monkeypatch.setattr(m, "Params", FakeParams)
  monkeypatch.setattr(m, "cloudlog", log)
  monkeypatch.setattr(m.messaging, "log_from_bytes", fake_log_from_bytes)
  monkeypatch.setattr(m, "_ATTEMPT_MARKER", str(tmp_path / "attempts"))
  monkeypatch.setattr(m, "_flash_attempted", False)
  yield log


@pytest.mark.parametrize("store, expected", [
  ({"CarPlatformBundle": {"brand": "byd"}}, True),
  ({"CarPlatformBundle": {"make": "BYD"}}, True),
  ({"CarPlatformBundle": {"platform": "BYD_SONG_PLUS"}}, True),
  ({"CarPlatformBundle": {"brand": "toyota"}}, False),
  ({"CarParamsPersistent": b"CP"}, True),
  ({"CarParamsPersistent": b"BROKEN"}, False),
])
def test_is_byd_sources(store, expected):
  FakeParams.store = store
  assert m._is_byd() is expected


def test_firmware_blob_is_the_panda_fw_base_pair():
  with open(m.FW_PATH, "rb") as f:
    code = f.read()
  with open(m.BOOTSTUB_PATH, "rb") as f:
    bootstub = f.read()
  assert hashlib.sha256(code).hexdigest() == m.FW_SHA256
  assert len(code) == 57924
  # pin the panda_fw_base@9d0a0046 DEBUG build by its embedded version string;
  # verified running on the target board on 2026-11-08 (see flasher docstring)
  assert b"DEV-9d0a0046-DEBUG" in code
  assert hashlib.sha256(bootstub).hexdigest() == m.BOOTSTUB_SHA256
  assert len(bootstub) == 14344


def test_flash_static_erases_right_sectors_and_writes_all_bytes():
  with open(FW, "rb") as f:
    code = f.read()
  handle = FakeHandle()
  m._flash_static(handle, code)

  assert sum(1 for a in handle.control_writes if a[1] == 0xb1) == 1
  assert [a[2] for a in handle.control_writes if a[1] == 0xb2] == [1, 2, 3, 4]
  assert sum(1 for a in handle.control_writes if a[1] == 0xd8) == 1
  assert len(handle.bulk_writes) == math.ceil(len(code) / 0x200)
  assert all(endpoint == 2 for endpoint, _ in handle.bulk_writes)
  assert max(len(data) for _, data in handle.bulk_writes) <= 0x200
  assert b"".join(data for _, data in handle.bulk_writes) == code


def test_happy_path_pair_flashes_via_dfu(isolated):
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  with open(BOOTSTUB, "rb") as f:
    bootstub_code = f.read()
  m.flash_byd_long(["spi-serial-0001"])

  assert len(FakeDFU.instances) == 1
  dfu = FakeDFU.instances[0]
  assert dfu.programmed == bootstub_code, "dev bootstub must be written over DFU"
  assert dfu.reset_called and dfu.closed

  panda = FakePanda.instances[0]
  assert not any(a[1] == 0xd1 for a in panda._handle.control_writes), "app-only flash through an unknown bootstub is never safe"
  with open(FW, "rb") as f:
    assert b"".join(d for _, d in panda._handle.bulk_writes) == f.read()
  assert panda.connects == 2  # bounded re-open after DFU and after flashing
  assert panda.closed
  assert m._read_attempts() == 1
  assert any("flashed and verified" in str(r) for r in isolated.records)


def test_up_to_date_panda_is_a_no_op(isolated):
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  FakePanda.up_to_date = True
  m.flash_byd_long(["spi-serial-0001"])

  assert len(FakeDFU.instances) == 0, "matching signature must not trigger a DFU cycle"
  assert not FakePanda.instances[0]._handle.bulk_writes
  assert m._read_attempts() == 0, "an up-to-date panda must not consume the per-boot quota"


def test_dfu_never_appears_fails_without_touching_app(isolated, monkeypatch):
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  monkeypatch.setattr(m, "DFU_ENTRY_WAIT_S", 0.05)
  monkeypatch.setattr(m.time, "sleep", lambda s: None)
  FakeDFU.fail_list = True
  m.flash_byd_long(["spi-serial-0001"])

  assert FakeDFU.instances == []
  assert not FakePanda.instances[0]._handle.bulk_writes, "must not write app bytes"
  assert any("FAILED" in str(r) for r in isolated.records), "failure must not be silent"
  assert m._read_attempts() == 1


def test_idempotent_within_process_and_capped_across_restarts():
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  m.flash_byd_long(["spi-serial-0001"])
  FakePanda.instances = []
  m.flash_byd_long(["spi-serial-0001"])
  assert FakePanda.instances == []
  assert m._read_attempts() == 1

  # manager.py restarting pandad.py resets the module-level flag; the runtime
  # marker must still stop a board stuck in bootstub from a reflash loop.
  m._flash_attempted = False
  with open(m._ATTEMPT_MARKER, "w") as f:
    f.write(str(m.MAX_ATTEMPTS_PER_BOOT))
  m.flash_byd_long(["spi-serial-0001"])
  assert FakePanda.instances == []


def test_non_byd_and_corrupt_params_are_skipped():
  FakeParams.store = {"CarPlatformBundle": {"brand": "toyota"}}
  m.flash_byd_long(["spi-serial-0001"])
  assert FakePanda.instances == []

  FakeParams.store = {"CarPlatformBundle": None, "CarParamsPersistent": b"BROKEN"}
  m.flash_byd_long(["spi-serial-0001"])
  assert FakePanda.instances == []


def test_non_uno_panda_is_not_flashed_and_does_not_consume_an_attempt():
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  FakePanda.hw_type = b'\x03'  # black panda
  m.flash_byd_long(["usb-serial"])
  assert len(FakePanda.instances) == 1
  assert not FakePanda.instances[0]._handle.bulk_writes
  assert m._read_attempts() == 0


@pytest.mark.parametrize("blob_path, sha_name, tmp_name", [
  ("FW_PATH", "FW_SHA256", "bad_fw.bin"),
  ("BOOTSTUB_PATH", "BOOTSTUB_SHA256", "bad_bootstub.bin"),
])
def test_checksum_mismatch_refuses_flash(monkeypatch, tmp_path, blob_path, sha_name, tmp_name):
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  bad = tmp_path / tmp_name
  bad.write_bytes(b"not a firmware")
  monkeypatch.setattr(m, blob_path, str(bad))
  m.flash_byd_long(["spi-serial-0001"])
  assert FakePanda.instances == []
  assert FakeDFU.instances == []


def test_board_in_bootstub_still_pair_flashes(isolated):
  # a prior interrupted flash can leave the board in bootstub - and bootstub
  # identity cannot be queried, so the DFU pair-flash is taken regardless
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  FakePanda.starts_in_bootstub = True
  m.flash_byd_long(["spi-serial-0001"])

  assert len(FakeDFU.instances) == 1, "bootstub identity is unknown, always pair-flash"
  with open(FW, "rb") as f:
    assert b"".join(d for _, d in FakePanda.instances[0]._handle.bulk_writes) == f.read()
  assert m._read_attempts() == 1


def test_bounded_wait_gives_up_when_board_never_returns(isolated, monkeypatch):
  # DFU bootstub write succeeds but the board never comes back: give up in
  # bounded time, never hang
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  monkeypatch.setattr(m, "BOOTSTUB_WAIT_S", 0.05)
  FakePanda.connect_raises = True
  m.flash_byd_long(["spi-serial-0001"])

  assert len(FakeDFU.instances) == 1, "bootstub was written over DFU"
  assert not FakePanda.instances[0]._handle.bulk_writes, "must not write app bytes"
  assert any("FAILED" in str(r) for r in isolated.records)

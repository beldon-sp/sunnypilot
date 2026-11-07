#!/usr/bin/env python3
"""Regression tests for the BYD longitudinal firmware OTA flasher.

No hardware is touched: Panda, Params, messaging and cloudlog are replaced with
fakes. The real firmware blob is read from the repo, so checksum, sector math
and write chunking are exactly what production uses.
"""
import hashlib
import math
import os

import pytest

from openpilot.sunnypilot.selfdrive.pandad import byd_long_flasher as m

FW = os.path.join(os.path.dirname(m.__file__), "byd_long_fw.bin.signed")


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


class FakePanda:
  REQUEST_IN = 0xc0
  hw_type = b'\x05'  # UNO, F4
  enters_bootstub = True
  instances = []
  sig_calls = 0
  expected_sig = b""

  def __init__(self, serial):
    self.serial = serial
    self._handle = FakeHandle()
    self.bootstub = False
    self.closed = False
    self.connects = 0
    FakePanda.instances.append(self)

  @classmethod
  def reset(cls):
    cls.instances = []
    cls.sig_calls = 0
    cls.hw_type = b'\x05'
    cls.enters_bootstub = True

  def get_type(self):
    return FakePanda.hw_type

  def get_signature(self):
    FakePanda.sig_calls += 1
    # first read is the pre-flash firmware, later reads are the flashed one
    return FakePanda.expected_sig if FakePanda.sig_calls >= 2 else b'\x00' * 128

  def close(self):
    self.closed = True

  def reconnect(self):
    # reconnect() ends up in connect(wait=True), which has no exit condition
    raise AssertionError("reconnect() must never be used by the BYD flasher")

  def connect(self, claim=True, wait=False):
    assert wait is False, "wait=True loops forever when the board never comes back"
    self.connects += 1
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
  FakeParams.store = {}
  log = FakeLog()
  monkeypatch.setattr(m, "Panda", FakePanda)
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


def test_firmware_blob_is_the_vendor_image():
  with open(m.FW_PATH, "rb") as f:
    code = f.read()
  assert hashlib.sha256(code).hexdigest() == m.FW_SHA256
  assert len(code) == 88688


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


def test_happy_path_flashes_internal_spi_panda(isolated):
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  m.flash_byd_long(["spi-serial-0001"])

  assert len(FakePanda.instances) == 1
  panda = FakePanda.instances[0]
  assert panda.connects == 2  # bounded re-open after bootstub entry and after flashing
  assert any(a[1] == 0xd1 and a[2] == 1 for a in panda._handle.control_writes)
  with open(FW, "rb") as f:
    assert b"".join(d for _, d in panda._handle.bulk_writes) == f.read()
  assert panda.closed
  assert m._read_attempts() == 1
  assert any("flashed and verified" in str(r) for r in isolated.records)


def test_stale_bootloader_writes_no_bytes(isolated):
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  FakePanda.enters_bootstub = False
  m.flash_byd_long(["spi-serial-0001"])

  panda = FakePanda.instances[0]
  assert not any(a[1] == 0xb1 for a in panda._handle.control_writes), "must not erase"
  assert not panda._handle.bulk_writes, "must not write app bytes"
  assert any("FAILED" in str(r) for r in isolated.records), "failure must not be silent"


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


def test_checksum_mismatch_refuses_flash(monkeypatch, tmp_path):
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  bad = tmp_path / "bad_fw.bin"
  bad.write_bytes(b"not a firmware")
  monkeypatch.setattr(m, "FW_PATH", str(bad))
  m.flash_byd_long(["spi-serial-0001"])
  assert FakePanda.instances == []

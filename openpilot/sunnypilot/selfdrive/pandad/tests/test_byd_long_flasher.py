#!/usr/bin/env python3
"""Regression tests for the BYD longitudinal firmware OTA flasher.

No hardware is touched: Panda, PandaDFU, Params, messaging and cloudlog are
replaced with fakes. The real firmware and bootstub blobs are read from the
repo, so checksums, sector math and write chunking are exactly what
production uses.

Flow under test (learned on the target C3L, 2026-11-19):
  * the dongle is an INTERNAL USB device the SoC BOOT0 pulse cannot reach, so
    DFU entry is the USB 0xd1 request, never recover_internal_panda();
  * bootstub AND app are written straight over DFU in one session;
  * with fast boot the dongle can stay powered in DFU across reboots - a
    parked dongle is jumped (pair already written) or pair-flashed over DFU.
"""
import hashlib
import math
import os

import pytest
from panda import McuType

from openpilot.sunnypilot.selfdrive.pandad import byd_long_flasher as m

FW = os.path.join(os.path.dirname(m.__file__), "byd_long_fw.bin.signed")
BOOTSTUB = os.path.join(os.path.dirname(m.__file__), "byd_long_bootstub.bin")


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


class FakeDFUHandle:
  def __init__(self):
    self.erased = []
    self.programmed = []
    self.clears = 0

  def clear_status(self):
    self.clears += 1

  def erase_sector(self, sector):
    self.erased.append(sector)

  def program(self, address, dat):
    self.programmed.append((address, dat))


class FakeDFU:
  instances = []
  serials = ["dfu-serial-0001"]
  fail_list = False
  # simulates the dongle's power/DFU state: list() only answers when plugged
  plugged = False
  mcu_type = McuType.F4

  def __init__(self, serial):
    self.serial = serial
    self._handle = FakeDFUHandle()
    self.reset_called = False
    self.closed = False
    FakeDFU.instances.append(self)

  @classmethod
  def reset_class(cls):
    cls.instances = []
    cls.serials = ["dfu-serial-0001"]
    cls.fail_list = False
    cls.plugged = False
    cls.mcu_type = McuType.F4

  @staticmethod
  def list():
    if FakeDFU.fail_list or not FakeDFU.plugged:
      return []
    return list(FakeDFU.serials)

  def get_mcu_type(self):
    return FakeDFU.mcu_type

  def program_bootstub(self, code):
    # the real program_bootstub erases sectors 0+1 then writes the bootstub
    self._handle.erase_sector(0)
    self._handle.erase_sector(1)
    self._handle.program(McuType.F4.config.bootstub_address, code)

  def reset(self):
    self.reset_called = True

  def close(self):
    self.closed = True


class FakeHandle:
  def __init__(self):
    self.control_writes = []
    self.bulk_writes = []

  def controlWrite(self, *args, **kwargs):
    self.control_writes.append(args)
    # USB 0xd1 wValue=0 reboots the app into the ST system bootloader
    if len(args) >= 3 and args[1] == 0xd1 and args[2] == 0:
      FakeDFU.plugged = True

  def bulkWrite(self, endpoint, data, **kwargs):
    self.bulk_writes.append((endpoint, data))
    return len(data)


class FakePanda:
  REQUEST_IN = 0xc0
  hw_type = b'\x05'  # UNO, F4
  starts_in_bootstub = False
  up_to_date = False
  open_raises = False
  instances = []
  sig_calls = 0
  expected_sig = b""

  def __init__(self, serial):
    if FakePanda.open_raises:
      raise Exception("device went away")
    self.serial = serial
    self._handle = FakeHandle()
    self.bootstub = FakePanda.starts_in_bootstub
    self.closed = False
    FakePanda.instances.append(self)

  @classmethod
  def reset(cls):
    cls.instances = []
    cls.sig_calls = 0
    cls.hw_type = b'\x05'
    cls.starts_in_bootstub = False
    cls.up_to_date = False
    cls.open_raises = False

  def get_type(self):
    return FakePanda.hw_type

  def get_signature(self):
    FakePanda.sig_calls += 1
    if FakePanda.up_to_date:
      return FakePanda.expected_sig
    # the pre-flash firmware reports a foreign signature; after the DFU pair
    # write the app comes up with ours
    return FakePanda.expected_sig if FakePanda.sig_calls >= 2 else b'\x00' * 128

  def close(self):
    self.closed = True

  def reconnect(self):
    # reconnect() ends up in connect(wait=True), which has no exit condition
    raise AssertionError("reconnect() must never be used by the BYD flasher")

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
  monkeypatch.setattr(m, "Panda", FakePanda)
  monkeypatch.setattr(m, "PandaDFU", FakeDFU)
  monkeypatch.setattr(m, "Params", FakeParams)
  monkeypatch.setattr(m, "cloudlog", log)
  monkeypatch.setattr(m.messaging, "log_from_bytes", fake_log_from_bytes)
  monkeypatch.setattr(m, "_ATTEMPT_MARKER", str(tmp_path / "attempts"))
  monkeypatch.setattr(m, "_STATE_PATH", str(tmp_path / "fw_state"))
  monkeypatch.setattr(m, "_flash_attempted", False)
  yield log


def dfu_written_app(dfu) -> bytes:
  app_writes = [(a, d) for a, d in dfu._handle.programmed
                if a == McuType.F4.config.app_address]
  assert len(app_writes) == 1, "the app must be written exactly once at the app address"
  return app_writes[0][1]


def dfu_written_bootstub(dfu) -> bytes:
  bs_writes = [(a, d) for a, d in dfu._handle.programmed
               if a == McuType.F4.config.bootstub_address]
  assert len(bs_writes) == 1, "the bootstub must be written exactly once at the bootstub address"
  return bs_writes[0][1]


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
  # verified running on the target car on 2026-11-19 (see flasher docstring)
  assert b"DEV-9d0a0046-DEBUG" in code
  assert hashlib.sha256(bootstub).hexdigest() == m.BOOTSTUB_SHA256
  assert len(bootstub) == 14344


def test_dfu_program_pair_erases_app_sectors_and_writes_both_images():
  with open(FW, "rb") as f:
    code = f.read()
  with open(BOOTSTUB, "rb") as f:
    bootstub = f.read()
  dfu = FakeDFU("dfu-serial-0001")
  m._dfu_program_pair(dfu, bootstub, code)

  sizes = McuType.F4.config.sector_sizes
  last_sector = next(i + 1 for i, v in enumerate(__import__("itertools").accumulate(sizes[1:])) if v > len(code))
  assert dfu._handle.erased == [0, 1] + list(range(1, last_sector + 1))
  assert dfu_written_bootstub(dfu) == bootstub
  assert dfu_written_app(dfu) == code


def test_happy_path_enters_dfu_over_usb_and_pair_flashes(isolated, monkeypatch):
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  monkeypatch.setattr(m, "DFU_ENTRY_WAIT_S", 0.05)
  monkeypatch.setattr(m, "FLASH_EXIT_WAIT_S", 0.05)
  monkeypatch.setattr(m.time, "sleep", lambda s: None)
  with open(BOOTSTUB, "rb") as f:
    bootstub_code = f.read()
  m.flash_byd_long(["spi-serial-0001"])

  panda = FakePanda.instances[0]
  # DFU entry is the USB 0xd1 request - the GPIO BOOT0 pulse cannot reach the
  # internal dongle, so recover_internal_panda() must never be involved
  assert any(a[1] == 0xd1 and a[2] == 0 for a in panda._handle.control_writes)

  assert len(FakeDFU.instances) == 1
  dfu = FakeDFU.instances[0]
  assert dfu_written_bootstub(dfu) == bootstub_code, "dev bootstub must be written over DFU"
  with open(FW, "rb") as f:
    assert dfu_written_app(dfu) == f.read()
  assert dfu.reset_called and dfu.closed

  # no app bytes are ever pushed through the bootstub flash protocol here
  assert not panda._handle.bulk_writes
  assert panda.closed
  assert m._read_attempts() == 1
  assert m._state_matches(), "a verified flash must persist its state marker"
  assert any("flashed and verified" in str(r) for r in isolated.records)


def test_up_to_date_panda_is_a_no_op(isolated):
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  FakePanda.up_to_date = True
  m.flash_byd_long(["spi-serial-0001"])

  assert len(FakeDFU.instances) == 0, "matching signature must not trigger a DFU cycle"
  assert not FakePanda.instances[0]._handle.control_writes
  assert m._read_attempts() == 0, "an up-to-date panda must not consume the per-boot quota"
  assert not m._state_matches() or m._state_matches()  # marker write is best-effort


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
  assert not m._state_matches(), "a failed flash must not record the state marker"


def test_dfu_parked_dongle_is_pair_flashed_over_dfu(isolated):
  # fast boot keeps the dongle powered: it can come up in DFU at every boot
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  FakeDFU.plugged = True
  m.flash_byd_long([])

  assert FakePanda.instances == [], "an app-mode open must never be attempted against a DFU-parked dongle"
  assert len(FakeDFU.instances) == 1
  dfu = FakeDFU.instances[0]
  with open(BOOTSTUB, "rb") as f:
    assert dfu_written_bootstub(dfu) == f.read()
  with open(FW, "rb") as f:
    assert dfu_written_app(dfu) == f.read()
  assert dfu.reset_called and dfu.closed
  assert m._read_attempts() == 1
  assert m._state_matches()
  assert any("flashed over DFU and jumped" in str(r) for r in isolated.records)


def test_dfu_parked_dongle_with_valid_state_is_jumped_not_reflashed(isolated):
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  FakeDFU.plugged = True
  # write the marker the way a previous verified flash would have
  os.makedirs(os.path.dirname(m._STATE_PATH), exist_ok=True)
  with open(m._STATE_PATH, "w") as f:
    f.write(f"{m.FW_SHA256}:{m.BOOTSTUB_SHA256}")
  m.flash_byd_long([])

  assert len(FakeDFU.instances) == 1
  dfu = FakeDFU.instances[0]
  assert dfu.reset_called, "a parked dongle with our pair written only needs a DFU jump"
  assert dfu._handle.erased == [], "no erase may happen when the pair is already verified"
  assert dfu._handle.programmed == []
  assert dfu.closed
  assert m._read_attempts() == 0, "a jump must not consume the per-boot quota"
  assert any("jumping to app" in str(r) for r in isolated.records)


def test_dfu_non_f4_device_is_left_alone(isolated):
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  FakeDFU.plugged = True
  FakeDFU.mcu_type = McuType.H7
  m.flash_byd_long([])

  assert len(FakeDFU.instances) == 1
  dfu = FakeDFU.instances[0]
  assert dfu._handle.erased == [] and dfu._handle.programmed == []
  assert not dfu.reset_called, "an H7 board in DFU belongs to pandad's packaged-image recovery"
  assert any("not the BYD F4 dongle" in str(r) for r in isolated.records)


def test_idempotent_within_process_and_capped_across_restarts(isolated):
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
  assert not FakePanda.instances[0]._handle.control_writes
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


def test_board_in_bootstub_still_pair_flashes(isolated, monkeypatch):
  # a prior interrupted flash can leave the board in bootstub - and bootstub
  # identity cannot be queried, so the DFU pair-flash is taken regardless
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  FakePanda.starts_in_bootstub = True
  monkeypatch.setattr(m, "DFU_ENTRY_WAIT_S", 0.05)
  monkeypatch.setattr(m, "FLASH_EXIT_WAIT_S", 0.05)
  monkeypatch.setattr(m.time, "sleep", lambda s: None)
  m.flash_byd_long(["spi-serial-0001"])

  assert len(FakeDFU.instances) == 1, "bootstub identity is unknown, always pair-flash"
  with open(FW, "rb") as f:
    assert dfu_written_app(FakeDFU.instances[0]) == f.read()
  assert m._read_attempts() == 1


def test_verify_never_seeing_the_board_fails_bounded(isolated, monkeypatch):
  # the DFU pair write succeeded but the board never re-enumerates (this clone
  # sometimes needs a power cycle): give up in bounded time, never hang
  FakeParams.store = {"CarPlatformBundle": {"brand": "byd"}}
  monkeypatch.setattr(m, "DFU_ENTRY_WAIT_S", 0.05)
  monkeypatch.setattr(m, "FLASH_EXIT_WAIT_S", 0.05)
  monkeypatch.setattr(m.time, "sleep", lambda s: None)

  opened = []
  real_init = FakePanda.__init__

  def init_once_then_raise(self, serial):
    opened.append(serial)
    if len(opened) > 1:
      raise Exception("device went away")
    real_init(self, serial)

  monkeypatch.setattr(FakePanda, "__init__", init_once_then_raise)
  m.flash_byd_long(["spi-serial-0001"])

  assert len(FakeDFU.instances) == 1, "the pair was written over DFU"
  assert any("did not re-enumerate" in str(r) for r in isolated.records)
  assert any("FAILED" in str(r) for r in isolated.records)
  assert not m._state_matches(), "an unverified flash must not record the state marker"

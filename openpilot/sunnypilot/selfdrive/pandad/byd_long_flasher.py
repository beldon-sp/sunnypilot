#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

BYD longitudinal firmware delivery over OTA (the device is never hand-flashed).

The clone DOS pandas in BYD Song Plus DM-i 2022 report hw_type UNO(0x05) or
DOS(0x06) and are STM32F4 based. sunnypilot's panda library only keeps H7
devices in SUPPORTED_DEVICES, so Panda.flash()/reset() refuse them and the MCU
never gets a firmware update. A panda without BYD_PARAM_LONGITUDINAL rejects
the whole 0x32D/E/F ACC stream, controls_allowed never latches, and selfdrived
raises controlsMismatch via mismatch_counter >= 200.

Delivered firmware: the architecture-matched panda_fw_base@9d0a0046 build
(DEV-9d0a0046-DEBUG, F4/UNO forced, USB VID 0x3801). It is a transparent ACC
replacement (no "byd acc inject" fwd hooks), so the host-spoofed 0x316 LKAS
stream passes through and lateral control works. Verified live on 2026-11-08:
set_safety_mode(35, 2) latches, safety_tx_blocked stays 0 (the vendor image
blocked ~50/s), pandad accepts the signature. Its health packet is carrot v17
(59 bytes); pandad's get_state() tells the three layouts apart by reply size,
see selfdrive/pandad/panda.h.

CRITICAL - bootstub/app signature pairing (learned 2026-11-08, see
docs/byd-panda-fw-build.md section 7): a DEV-signed app is only accepted by a
dev bootstub built from the same commit (ALLOW_DEBUG). A vendor release
bootstub rejects it, and bootstub.c's fail() drops the board into the USB soft
flasher, which never leaves bootstub - that looks exactly like the app
"crash-looping" but the app never runs. bootstub.c has no version endpoint, so
the bootstub's identity cannot be queried over USB. Therefore the ONLY safe
update path is the unconditional DFU pair-flash: force DFU via BOOT0, write
our dev bootstub, then flash the app through it.

This module mirrors rivian_long_flasher.py: the firmware lives in the main
repo (so OTA carries it) and flashing bypasses the SUPPORTED_DEVICES gate.
pandad.py's flash_panda() skips UNO/DOS devices, so the H7 firmware can never
overwrite this one.

Two differences from the USB-only Rivian kit matter here:
  * these clones may present the DOS/UNO panda over SPI as an internal panda,
    so all of Panda.list() is scanned (USB + SPI), and the write chunk matches
    Panda.flash_static()/the vendor flasher (0x200, not 0x10);
  * Panda.reconnect() calls connect(wait=True), which loops forever when a
    stale bootloader drops to the system-memory bootloader instead of coming
    back as a panda. Every re-open here is bounded by wait_for_link() instead.
"""
import hashlib
import os
import time
from itertools import accumulate

from openpilot.cereal import messaging
from opendbc.car.structs import car
from panda import Panda, PandaDFU
from openpilot.common.hardware import HARDWARE
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog

FW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "byd_long_fw.bin.signed")
BOOTSTUB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "byd_long_bootstub.bin")
# panda_fw_base@9d0a0046 build (DEV-9d0a0046-DEBUG) - verified running on the
# target board on 2026-11-08 after the paired DFU flash below. FW and bootstub
# MUST come from the same scons run: the bootstub verifies the app signature,
# and the dev bootstub is the only one that accepts a DEV-signed app.
FW_SHA256 = "af5174fcf8e0f3f6faca28299903c4ce7c3e2781db403bfbe029c4d4348f75b6"
BOOTSTUB_SHA256 = "eefd4cc8d513f8f44de43ea620e6fa16e05506066d2068d738e85c028fde7dd4"
SECTOR_SIZES = [0x4000] * 4 + [0x10000] + [0x20000] * 11
BYD_LEGACY_HW_TYPES = (b'\x05', b'\x06')  # uno, dos
DFU_ENTRY_WAIT_S = 20
BOOTSTUB_WAIT_S = 15
FLASH_EXIT_WAIT_S = 15

# _flash_attempted only lives as long as the pandad.py process. manager.py can
# restart that process, so keep a small runtime counter in /dev/shm (cleared on
# every device reboot) to stop a board stuck in bootstub from being reflashed
# once a second forever. Writing here is runtime state, not a persistent Param.
# NOTE: some clone devices restore /dev/shm from a persistent snapshot at boot
# (observed 2026-11-08: marker file mtime months old after reboot), so the
# marker name is suffixed with the kernel boot_id - a restored stale file can
# never match the current boot.
def _boot_scoped_marker() -> str:
  try:
    with open("/proc/sys/kernel/random/boot_id") as f:
      boot_id = f.read().strip().replace("-", "")[:12]
  except Exception:
    boot_id = "unknownboot"
  return f"/dev/shm/byd_long_flash_attempts.{boot_id}"


MAX_ATTEMPTS_PER_BOOT = 3
_ATTEMPT_MARKER = _boot_scoped_marker()
_flash_attempted = False


def _read_attempts() -> int:
  try:
    with open(_ATTEMPT_MARKER) as f:
      return int(f.read().strip())
  except Exception:
    return 0


def _record_attempt() -> None:
  global _flash_attempted
  _flash_attempted = True
  try:
    with open(_ATTEMPT_MARKER, "w") as f:
      f.write(str(_read_attempts() + 1))
  except Exception:
    pass


def _is_byd() -> bool:
  params = Params()

  # Params.get() deserializes JSON keys, but a corrupt/empty value must never
  # escape into pandad.py's setup loop (an exception there skips ./pandad).
  try:
    bundle = params.get("CarPlatformBundle")
  except Exception:
    cloudlog.exception("BYD flasher: failed to read CarPlatformBundle")
    bundle = None
  if isinstance(bundle, dict):
    # vendor bundles use "make"/"platform" rather than "brand"
    if str(bundle.get("brand", "")).lower() == "byd" or str(bundle.get("make", "")).lower() == "byd":
      return True
    if str(bundle.get("platform", "")).upper().startswith("BYD_"):
      return True

  # Auto-fingerprinted cars have no bundle; fall back to the last known CarParams.
  try:
    CP_bytes = params.get("CarParamsPersistent")
    if CP_bytes:
      CP = messaging.log_from_bytes(CP_bytes, car.CarParams)
      if str(CP.brand).lower() == "byd":
        return True
  except Exception:
    cloudlog.exception("BYD flasher: failed to deserialize CarParamsPersistent")

  return False


def wait_for_link(panda: Panda, timeout: float) -> bool:
  """Bounded re-open. Never use reconnect(): it ends up in connect(wait=True),
  which has no exit when the board comes back as the system-memory bootloader."""
  t_start = time.monotonic()
  while (time.monotonic() - t_start) < timeout:
    try:
      panda.connect(claim=True, wait=False)
      return True
    except Exception:
      time.sleep(0.1)
  return False


def _dfu_pair_flash(bootstub_code: bytes) -> None:
  """Write our dev bootstub over DFU. Erases every app sector as a side
  effect; the caller must flash the app right after."""
  deadline = time.monotonic() + DFU_ENTRY_WAIT_S
  while True:
    HARDWARE.recover_internal_panda()  # BOOT0 high + reset = ST system bootloader
    time.sleep(0.5)
    serials = PandaDFU.list()
    if serials:
      break
    if time.monotonic() > deadline:
      raise RuntimeError("BYD panda did not enter DFU mode")
  cloudlog.info(f"BYD panda in DFU mode ({serials[0]}), writing paired dev bootstub")
  dfu = PandaDFU(serials[0])
  try:
    # erases all sectors, programs the bootstub at 0x08000000, then jumps to it
    dfu.program_bootstub(bootstub_code)
    dfu.reset()
  finally:
    dfu.close()


def _flash_static(handle, code):
  assert Panda.flasher_present(handle)
  last_sector = next((i + 1 for i, v in enumerate(accumulate(SECTOR_SIZES[1:])) if v > len(code)), -1)
  assert 1 <= last_sector < 7, "Invalid firmware size"

  handle.controlWrite(Panda.REQUEST_IN, 0xb1, 0, 0, b'')
  for i in range(1, last_sector + 1):
    handle.controlWrite(Panda.REQUEST_IN, 0xb2, i, 0, b'')
  # 0x200 matches Panda.flash_static() and the vendor F4 flasher. The 16-byte
  # chunks used by the USB-only Rivian kit would mean >5500 SPI transfers for
  # an 88 KB image, which can outlive the bootstub's transfer window.
  STEP = 0x200
  for i in range(0, len(code), STEP):
    handle.bulkWrite(2, code[i:i + STEP])
  try:
    handle.controlWrite(Panda.REQUEST_IN, 0xd8, 0, 0, b'', expect_disconnect=True)
  except Exception:
    pass


def _flash_panda(panda: Panda, code: bytes, bootstub_code: bytes, serial: str) -> None:
  expected_sig = Panda.get_signature_from_firmware(FW_PATH)
  if not panda.bootstub and panda.get_signature() == expected_sig:
    cloudlog.info("BYD panda already running longitudinal firmware")
    return

  # count before touching hardware so a failure can never loop-flash;
  # an already-up-to-date panda must not consume the per-boot quota
  _record_attempt()

  # The bootstub's identity cannot be queried (no version endpoint in
  # bootstub.c) and a vendor bootstub rejects our DEV-signed app, so an
  # app-only flash through an unknown bootstub is never safe: always go the
  # DFU pair-flash route (docs/byd-panda-fw-build.md sections 5 and 7).
  cloudlog.info(f"Pair-flashing BYD longitudinal firmware (bootstub+app) to F4 panda {serial} via DFU")
  _dfu_pair_flash(bootstub_code)

  panda.close()
  # DFU erased everything: the board must come back in OUR bootstub.
  if not wait_for_link(panda, BOOTSTUB_WAIT_S) or not panda.bootstub:
    raise RuntimeError("BYD panda did not return as bootstub after DFU bootstub flash")

  _flash_static(panda._handle, code)
  panda.close()
  if not wait_for_link(panda, FLASH_EXIT_WAIT_S) or panda.bootstub:
    raise RuntimeError("BYD panda did not leave bootstub after flashing")
  if panda.get_signature() != expected_sig:
    raise RuntimeError("BYD longitudinal firmware signature mismatch after flashing")
  cloudlog.info("BYD longitudinal firmware flashed and verified")


def flash_byd_long(panda_serials: list[str]) -> None:
  global _flash_attempted
  if _flash_attempted or _read_attempts() >= MAX_ATTEMPTS_PER_BOOT or not os.path.isfile(FW_PATH):
    return

  try:
    if not _is_byd():
      return
  except Exception:
    # never let a params/cereal failure take down pandad.py's setup loop
    cloudlog.exception("BYD flasher: vehicle brand check failed, skipping")
    return

  blobs = {}
  for name, path, sha in (("firmware", FW_PATH, FW_SHA256), ("bootstub", BOOTSTUB_PATH, BOOTSTUB_SHA256)):
    try:
      with open(path, "rb") as f:
        code = f.read()
    except Exception:
      cloudlog.exception(f"BYD flasher: cannot read {name}")
      _flash_attempted = True
      return
    if hashlib.sha256(code).hexdigest() != sha:
      cloudlog.error(f"BYD longitudinal {name} checksum mismatch, refusing to flash")
      _flash_attempted = True
      return
    blobs[name] = code

  # Panda.list() already covers USB *and* SPI. The DOS/UNO panda in C3L clones
  # is treated as an internal panda by pandad.py, so filtering on usb_list()
  # would silently skip the very device we need to upgrade.
  for serial in panda_serials:
    try:
      panda = Panda(serial)
    except Exception:
      cloudlog.exception(f"BYD flasher: cannot open panda {serial}")
      continue
    try:
      try:
        hw_type = panda.get_type()
      except Exception:
        cloudlog.exception(f"BYD flasher: cannot read hw type of {serial}")
        continue
      if hw_type not in BYD_LEGACY_HW_TYPES:
        continue
      _flash_panda(panda, blobs["firmware"], blobs["bootstub"], serial)
      return
    except Exception as e:
      # A failed flash is never silent: the board may be in bootstub or DFU and
      # needs the paired DFU recovery (docs/byd-panda-fw-build.md section 5).
      hint = "Recover with the paired DFU flow: recover_internal_panda() -> PandaDFU.recover() -> BOOT0=0 -> flash app."
      cloudlog.exception(f"BYD longitudinal firmware flash FAILED for {serial}: {e}. {hint}")
      return
    finally:
      try:
        panda.close()
      except Exception:
        pass

#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

BYD longitudinal firmware delivery over OTA (the device is never hand-flashed).

The BYD Song Plus DM-i 2022 C3L clone carries a DOS/UNO F4 panda as an INTERNAL
USB device (it enumerates on the device's own USB bus, not an external port),
reports hw_type UNO(0x05) or DOS(0x06), and is STM32F4 based. sunnypilot's
panda library only keeps H7 devices in SUPPORTED_DEVICES, so Panda.flash()/
reset() refuse it and the MCU never gets a firmware update from pandad's
flash_panda(). A panda whose safety model lacks the BYD_PARAM_LONGITUDINAL
behavior rejects the host's 0x32D/E/F ACC stream, controls_allowed never
latches, and selfdrived raises controlsMismatch / bydLongFirmwareMissing.

Delivered firmware: the architecture-matched panda_fw_base@9d0a0046 build
(DEV-9d0a0046-DEBUG, F4/UNO forced, USB VID 0x3801). Verified live 2026-11-19
on the target car: pandad sets safety model 35 with the LONGITUDINAL param and
safetyTxBlocked stays 0.

Entry into DFU (learned 2026-11-19 on the target C3L):
  * The SoC GPIO BOOT0 pulse (recover_internal_panda, GPIO 124/134) is wired to
    the comma-three internal panda, NOT to this USB dongle - it can never put
    the dongle into DFU. Do not rely on it.
  * USB request 0xd1 (wValue=0, Panda.reset(enter_bootloader=True)) makes the
    app firmware reboot into the ST system bootloader. This is the reliable
    entry and is used as the primary path here.
  * With the device's "fast boot" enabled, USB power survives device reboots,
    so a dongle parked in DFU stays in DFU across reboots and re-enumerates
    once the USB host stack comes back. flash_byd_long() therefore handles
    "found in DFU" as a first-class state: jump to the app if our pair is
    already written (persistent state marker), full pair-flash otherwise.
  * Re-enumeration after an MCU-initiated USB change can be slow or need a
    power cycle on this clone. Every wait is bounded, and the app is written
    STRAIGHT over DFU (erase_sector + program) in the same DFU session as the
    bootstub, so the board never has to come back as bootstub mid-flash.

CRITICAL - bootstub/app signature pairing (see docs/byd-panda-ota-flash.md): a
DEV-signed app is only accepted by a dev bootstub built from the same commit
(ALLOW_DEBUG). The bootstub's identity cannot be queried over USB, so the
app is never flashed through an unknown bootstub - always the DFU pair route.

This module mirrors rivian_long_flasher.py: the firmware lives in the main
repo (so OTA carries it) and flashing bypasses the SUPPORTED_DEVICES gate.
pandad.py's flash_panda() skips UNO/DOS devices, so the H7 firmware can never
overwrite this one.
"""
import hashlib
import os
import time
from itertools import accumulate

from openpilot.cereal import messaging
from opendbc.car.structs import car
from panda import Panda, PandaDFU, McuType
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog

FW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "byd_long_fw.bin.signed")
BOOTSTUB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "byd_long_bootstub.bin")
# panda_fw_base@9d0a0046 build (DEV-9d0a0046-DEBUG) - verified running on the
# target car on 2026-11-19 (see module docstring). FW and bootstub MUST come
# from the same scons run: the bootstub verifies the app signature, and the dev
# bootstub is the only one that accepts a DEV-signed app.
FW_SHA256 = "af5174fcf8e0f3f6faca28299903c4ce7c3e2781db403bfbe029c4d4348f75b6"
BOOTSTUB_SHA256 = "eefd4cc8d513f8f44de43ea620e6fa16e05506066d2068d738e85c028fde7dd4"
BYD_LEGACY_HW_TYPES = (b'\x05', b'\x06')  # uno, dos
DFU_ENTRY_WAIT_S = 25
FLASH_EXIT_WAIT_S = 20

# Persistent state marker (NOT /dev/shm): with fast boot the dongle never loses
# power, so "parked in DFU" can persist across many reboots. The marker records
# that this exact bootstub+app pair has been verified written, letting a parked
# dongle be started with a plain DFU jump instead of a full reflash.
#
# /data/persist is preferred (dedicated persist partition), but AGNOS OTA
# upgrades and reflashes reset its ownership to root:root 0755 (observed
# 2026-11-22: PermissionError on every boot until a manual sudo chown). The
# fallback lives under /data/media/0, which is comma-writable by construction
# on AGNOS (loggerd writes routes there) and survives reboots and OTA upgrades.
# Write goes to the first writable candidate; a match in ANY candidate counts.
_STATE_CANDIDATES = [
  "/data/persist/byd_long_fw_state",
  "/data/media/0/byd_long_fw_state",
]


def _state_matches() -> bool:
  expected = f"{FW_SHA256}:{BOOTSTUB_SHA256}"
  for path in _STATE_CANDIDATES:
    try:
      with open(path) as f:
        if f.read().strip() == expected:
          return True
    except Exception:
      pass
  return False


def _record_state() -> None:
  for path in _STATE_CANDIDATES:
    try:
      os.makedirs(os.path.dirname(path), exist_ok=True)
      with open(path, "w") as f:
        f.write(f"{FW_SHA256}:{BOOTSTUB_SHA256}")
      cloudlog.info(f"BYD flasher: persistent state marker written to {path}")
      return
    except Exception:
      continue
  cloudlog.exception("BYD flasher: cannot write persistent state marker in any candidate location")


# _flash_attempted only lives as long as the pandad.py process. manager.py can
# restart that process, so keep a small runtime counter in /dev/shm (cleared on
# every device reboot) to stop a board stuck in bootstub from being reflashed
# once a second forever. Writing here is runtime state, not a persistent Param.
# NOTE: some clone devices restore /dev/shm from a persistent snapshot at boot
# (observed 2026-11-08: marker file mtime months old after reboot), so the
# marker name is suffixed with the kernel boot_id - a restored stale file can
# never match the current boot. Some clones additionally keep a stable boot_id
# across reboots (observed 2026-11-19: a months-old marker matching the live
# boot_id), so _read_attempts() also treats an old marker as expired: without
# that, the quota accumulates across reboots and the flasher locks out
# permanently after 3 total attempts - the exact recurrence this module
# exists to prevent.
def _boot_scoped_marker() -> str:
  try:
    with open("/proc/sys/kernel/random/boot_id") as f:
      boot_id = f.read().strip().replace("-", "")[:12]
  except Exception:
    boot_id = "unknownboot"
  return f"/dev/shm/byd_long_flash_attempts.{boot_id}"


MAX_ATTEMPTS_PER_BOOT = 3
# a runtime marker older than this cannot be from the current provisioning
# session; re-provisioning is idempotent and signature-guarded, so erring
# toward "expired" is always the safe direction
_MARKER_STALE_S = 3600
_ATTEMPT_MARKER = _boot_scoped_marker()
_flash_attempted = False


def _read_attempts() -> int:
  try:
    if time.time() - os.path.getmtime(_ATTEMPT_MARKER) > _MARKER_STALE_S:
      return 0  # stale/snapshot-restored marker - never lock out on it
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


def _dfu_program_pair(dfu: PandaDFU, bootstub_code: bytes, fw_code: bytes) -> None:
  """Write the bootstub AND the app straight over DFU, in one DFU session.

  The packaged panda lib only knows program_bootstub(); the app is written
  with the same DFU primitives (erase_sector + program). Doing both here means
  the board never has to re-enumerate as bootstub mid-flash - on C3L clones an
  MCU-initiated USB re-enumeration can take a full power cycle to recover."""
  mcu = dfu.get_mcu_type()
  assert mcu == McuType.F4, f"refusing to write the F4 sector map to a {mcu.name} board"

  dfu.program_bootstub(bootstub_code)

  handle = dfu._handle
  sizes = mcu.config.sector_sizes
  last_sector = next((i + 1 for i, v in enumerate(accumulate(sizes[1:])) if v > len(fw_code)), -1)
  assert 1 <= last_sector < 7, "Invalid firmware size"
  for i in range(1, last_sector + 1):
    handle.clear_status()
    handle.erase_sector(i)
  handle.clear_status()
  handle.program(mcu.config.app_address, fw_code)


def _dfu_recover_dongle(dfu_serials: list[str], blobs: dict) -> None:
  """A dongle found in DFU mode (fast boot keeps it powered across reboots).

  If our pair is already verified written (state marker), a plain DFU jump
  starts it; otherwise the pair is flashed over DFU and then jumped. The jump
  is safe and idempotent, so it must NEVER be blocked by the attempt quota -
  only the erase/write branch is."""
  dfu = PandaDFU(dfu_serials[0])
  try:
    try:
      mcu = dfu.get_mcu_type()
    except Exception:
      cloudlog.exception(f"BYD flasher: cannot read MCU type of DFU device {dfu_serials[0]}")
      return
    if mcu != McuType.F4:
      # e.g. an H7 panda parked in DFU; pandad's packaged-image recovery owns those
      cloudlog.info(f"DFU device {dfu_serials[0]} is {mcu.name}, not the BYD F4 dongle - leaving it alone")
      return

    if _state_matches():
      cloudlog.info("BYD dongle parked in DFU with the longitudinal pair already written - jumping to app")
      dfu.reset()
      return

    if _flash_attempted or _read_attempts() >= MAX_ATTEMPTS_PER_BOOT:
      cloudlog.warning(f"BYD dongle parked in DFU needs a pair flash, but the "
                       f"attempt quota is exhausted this boot ({_read_attempts()}/{MAX_ATTEMPTS_PER_BOOT}) - "
                       "next boot retries")
      return

    _record_attempt()
    cloudlog.info(f"Flashing BYD longitudinal pair straight over DFU ({dfu_serials[0]})")
    _dfu_program_pair(dfu, blobs["bootstub"], blobs["firmware"])
    dfu.reset()
    _record_state()
    cloudlog.info("BYD longitudinal pair flashed over DFU and jumped to app")
  except Exception as e:
    # A failed flash is never silent: the board may be in bootstub or DFU and
    # needs the paired DFU flow (docs/byd-panda-ota-flash.md).
    hint = "Recover with the paired DFU flow: 0xd1 or power-cycle into DFU -> flash pair -> jump."
    cloudlog.exception(f"BYD longitudinal firmware flash FAILED for DFU device: {e}. {hint}")
  finally:
    try:
      dfu.close()
    except Exception:
      pass


def _software_dfu_entry(panda: Panda) -> bool:
  """USB request 0xd1 (wValue=0) makes the app reboot into the ST system
  bootloader. The SoC GPIO BOOT0 pulse does not reach the C3L's internal USB
  dongle, so this is the only software entry."""
  try:
    panda._handle.controlWrite(Panda.REQUEST_IN, 0xd1, 0, 0, b'', expect_disconnect=True)
  except Exception:
    pass  # the device drops the bus as it reboots into DFU
  try:
    panda.close()
  except Exception:
    pass

  deadline = time.monotonic() + DFU_ENTRY_WAIT_S
  while time.monotonic() < deadline:
    if PandaDFU.list():
      return True
    time.sleep(0.5)
  return False


def _flash_panda(panda: Panda, blobs: dict, serial: str) -> None:
  expected_sig = Panda.get_signature_from_firmware(FW_PATH)
  if not panda.bootstub and panda.get_signature() == expected_sig:
    cloudlog.info("BYD panda already running longitudinal firmware")
    _record_state()
    return

  # count before touching hardware so a failure can never loop-flash;
  # an already-up-to-date panda must not consume the per-boot quota
  _record_attempt()

  if not _software_dfu_entry(panda):
    raise RuntimeError("BYD panda did not enter DFU mode via USB 0xd1 "
                       "(the SoC BOOT0 pulse does not reach the C3L internal dongle)")

  dfu_serials = PandaDFU.list()
  dfu = PandaDFU(dfu_serials[0])
  try:
    _dfu_program_pair(dfu, blobs["bootstub"], blobs["firmware"])
    dfu.reset()
  finally:
    try:
      dfu.close()
    except Exception:
      pass

  # bounded re-enumeration + signature verify; on this clone the re-enumeration
  # after a jump can be slow, and a power cycle may be needed - the next setup
  # pass (or next boot) then sees the running pair and verifies it instead
  deadline = time.monotonic() + FLASH_EXIT_WAIT_S
  while time.monotonic() < deadline:
    try:
      p = Panda(serial)
      try:
        if not p.bootstub:
          if p.get_signature() == expected_sig:
            _record_state()
            cloudlog.info("BYD longitudinal firmware flashed and verified")
            return
          raise RuntimeError("BYD longitudinal firmware signature mismatch after flashing")
      finally:
        p.close()
    except RuntimeError:
      raise
    except Exception:
      pass
    time.sleep(0.5)
  raise RuntimeError("BYD panda did not re-enumerate as the flashed app within the wait window")


def flash_byd_long(panda_serials: list[str]) -> None:
  global _flash_attempted
  if not os.path.isfile(FW_PATH):
    return

  # a dongle parked in DFU never appears in Panda.list(), so check DFU first;
  # on this rig the dongle IS the (only) panda. Handled BEFORE the brand gate
  # AND the attempt quota, for two hard reasons learned on real hardware:
  #   * quota: the jump-back must stay reachable even when this boot has burned
  #     its quota - otherwise a sticky runtime marker would strand a correctly
  #     flashed dongle in DFU forever (openpilot then boots with no panda).
  #   * brand: on a freshly wiped or brand-new C3L there are NO BYD params yet
  #     (fingerprinting needs a connected panda's CAN). Gating the DFU path on
  #     _is_byd() dead-ends: no panda -> no fingerprint -> no params -> never
  #     provisioned. A DFU-parked UNO/DOS F4 dongle under a byd-tici release is
  #     the provisioning target by construction, and flashing the transparent
  #     build is harmless to any other car (no engagement without a matching
  #     fingerprint).
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

  dfu_serials = PandaDFU.list()
  if dfu_serials:
    _dfu_recover_dongle(dfu_serials, blobs)
    return

  # the app-mode 0xd1 flash is the invasive path: keep the vehicle brand gate
  try:
    if not _is_byd():
      return
  except Exception:
    # never let a params/cereal failure take down pandad.py's setup loop
    cloudlog.exception("BYD flasher: vehicle brand check failed, skipping")
    return

  if _flash_attempted or _read_attempts() >= MAX_ATTEMPTS_PER_BOOT:
    return

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
      _flash_panda(panda, blobs, serial)
      return
    except Exception as e:
      # A failed flash is never silent: the board may be in bootstub or DFU and
      # needs the paired DFU flow (docs/byd-panda-ota-flash.md).
      hint = "Recover with the paired DFU flow: 0xd1 or power-cycle into DFU -> flash pair -> jump."
      cloudlog.exception(f"BYD longitudinal firmware flash FAILED for {serial}: {e}. {hint}")
      return
    finally:
      try:
        panda.close()
      except Exception:
        pass

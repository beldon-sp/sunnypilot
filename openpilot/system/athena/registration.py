#!/usr/bin/env python3
from pathlib import Path

from openpilot.common.params import Params
from openpilot.selfdrive.selfdrived.alertmanager import set_offroad_alert
from openpilot.common.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog


UNREGISTERED_DONGLE_ID = "UnregisteredDevice"

def is_registered_device() -> bool:
  dongle = Params().get("DongleId")
  return dongle not in (None, UNREGISTERED_DONGLE_ID)


def register(show_spinner=False) -> str | None:
  """
  BYD C3L clone: registration with the comma.ai backend is skipped
  entirely — there is no usable IMEI and pilotauth is unreachable,
  so the original flow would block startup forever. A cached dongle
  id (params or /persist) is still honored.

  With an unregistered device, manager skips athenad and the
  uploader, and the UI skips the prime API calls.
  """
  params = Params()

  dongle_id: str | None = params.get("DongleId")
  if dongle_id is None and Path(Paths.persist_root()+"/comma/dongle_id").is_file():
    # not all devices will have this; added early in comma 3X production (2/28/24)
    with open(Paths.persist_root()+"/comma/dongle_id") as f:
      dongle_id = f.read().strip()

  if dongle_id is None:
    dongle_id = UNREGISTERED_DONGLE_ID
    cloudlog.warning("skipping device registration with comma.ai backend")

  if dongle_id:
    params.put("DongleId", dongle_id, block=True)
    # BYD C3L clone: registration is skipped by design, so never surface the
    # unregistered-hardware alert (also clears any stale one).
    set_offroad_alert("Offroad_UnregisteredHardware", False)
  return dongle_id


if __name__ == "__main__":
  print(register())

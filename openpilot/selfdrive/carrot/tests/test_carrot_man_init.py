"""CarrotMan construction smoke test.

db5bf3146 stripped the HTTP7713 server but left a reference to the
now-undefined NAVI_HTTP_PORT at carrot_man.py:217. Pre-existing tests only
reached the SubMaster constructor, so the NameError escaped to the device.
This test fully instantiates CarrotMan() and exercises make_send_message().
"""
import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from openpilot.selfdrive.carrot import carrot_man as carrot_man_module
from openpilot.selfdrive.carrot.carrot_man import CarrotMan

MODULE = "openpilot.selfdrive.carrot.carrot_man"


class FakeParams:
  def __init__(self, path=None):
    self.path = path

  def get(self, key, *args, **kwargs):
    return ""

  def get_bool(self, key, default=False):
    # IsOffroad=True keeps make_send_message() off the onroad branch.
    return key == "IsOffroad"

  def get_int(self, key, default=0):
    return default


class FakeSubMaster:
  def __init__(self, services):
    self.seen = {s: False for s in services}
    self.alive = {s: False for s in services}
    self.valid = {s: False for s in services}
    self.updated = {s: False for s in services}
    self.recv_time = {s: 0.0 for s in services}
    self.logMonoTime = {s: 0 for s in services}

  def __getitem__(self, key):
    return None

  def update(self, timeout=0):
    pass


class FakePubMaster:
  def __init__(self, services):
    self.services = services

  def send(self, *args, **kwargs):
    pass


class FakeCarrotServ:
  traffic_state = 0
  xDistToTurn = 0.0
  xSpdDist = 0.0


def _instantiate():
  """Build a fully-stubbed CarrotMan() with every external resource patched."""
  with ExitStack() as stack:
    stack.enter_context(patch(f"{MODULE}.Params", FakeParams))
    stack.enter_context(patch(f"{MODULE}.get_gps_location_service", return_value="gpsLocationExternal"))
    stack.enter_context(patch(f"{MODULE}.messaging.SubMaster", FakeSubMaster))
    stack.enter_context(patch(f"{MODULE}.messaging.PubMaster", FakePubMaster))
    stack.enter_context(patch(f"{MODULE}.CarrotServ", return_value=FakeCarrotServ()))
    stack.enter_context(patch(f"{MODULE}.VisionCurveSpeed", return_value=object()))
    stack.enter_context(patch.object(CarrotMan, "get_broadcast_address", return_value="127.0.0.1"))
    stack.enter_context(patch.object(CarrotMan, "carrot_curve_speed_params", new=lambda self: None))
    return CarrotMan()


def test_carrot_man_init_smoke():
  cm = _instantiate()  # must not raise NameError at carrot_man.py:217
  assert hasattr(cm, "carrot_navi_http_port")
  assert cm.carrot_navi_http_port == 7713


def test_make_send_message_includes_legacy_navi_http_port():
  cm = _instantiate()
  payload = cm.make_send_message()
  msg = json.loads(payload)  # make_send_message() returns a JSON string, not a dict
  assert "navi_http_port" in msg
  assert msg["navi_http_port"] == 7713


def test_missing_navi_http_port_raises_name_error(monkeypatch):
  # Reverse check: removing the constant must reproduce the startup NameError,
  # proving the smoke test is not a rubber stamp. monkeypatch restores it.
  if hasattr(carrot_man_module, "NAVI_HTTP_PORT"):
    monkeypatch.delattr(carrot_man_module, "NAVI_HTTP_PORT")
  with pytest.raises(NameError, match="NAVI_HTTP_PORT"):
    _instantiate()

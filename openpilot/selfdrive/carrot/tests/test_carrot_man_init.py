"""CarrotMan construction + broadcast-loop smoke tests.

db5bf3146 stripped several carrot features but left dangling references
(NAVI_HTTP_PORT, put_nonblocking, queue_carrot_exception_tmux_send,
traceback, V_CURVE_LOOKUP_*). Pre-existing tests only reached the SubMaster
constructor, so the NameErrors escaped to the device. These tests fully
instantiate CarrotMan(), run make_send_message(), execute one tick of
broadcast_version_info(), and guard against undefined names coming back.
"""
import ast
import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openpilot.selfdrive.carrot import carrot_man as carrot_man_module
from openpilot.selfdrive.carrot import carrot_serv as carrot_serv_module
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

  def update_navi(self, *args, **kwargs):
    pass


class _LoopGuard(BaseException):
  """Raised by the stubbed time.sleep so a failing broadcast tick cannot hang."""


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


def test_broadcast_version_info_one_tick():
  # Run one tick of the 20Hz loop: no AttributeError / NameError / UnknownKeyName.
  # The old crash chain was put_nonblocking -> traceback -> tmux-uploader.
  cm = _instantiate()
  cm.is_running = True

  class FakeSocket:
    def setsockopt(self, *args, **kwargs):
      pass

    def sendto(self, *args, **kwargs):
      pass

    def close(self):
      pass

  class FakeRatekeeper:
    def __init__(self, *args, **kwargs):
      pass

    def keep_time(self):
      cm.is_running = False  # one tick, then leave the while loop

  guard = _LoopGuard()
  with ExitStack() as stack:
    stack.enter_context(patch(f"{MODULE}.socket.socket", return_value=FakeSocket()))
    stack.enter_context(patch(f"{MODULE}.Ratekeeper", FakeRatekeeper))
    stack.enter_context(patch(f"{MODULE}.PC", True))
    stack.enter_context(patch(f"{MODULE}.time.sleep", side_effect=guard))
    stack.enter_context(patch.object(CarrotMan, "carrot_curve_speed", return_value=0.0))
    stack.enter_context(patch.object(CarrotMan, "carrot_navi_route", return_value=([], [], 300)))
    stack.enter_context(patch.object(cm, "get_broadcast_address", return_value="127.0.0.1"))
    stack.enter_context(patch.object(cm, "get_local_ip", return_value="127.0.0.1"))
    print_exc = stack.enter_context(patch.object(carrot_man_module.traceback, "print_exc"))
    try:
      cm.broadcast_version_info()
    except _LoopGuard:
      pytest.fail("broadcast_version_info() reached its exception branch (time.sleep(1) called)")

    assert print_exc.call_count == 0, "broadcast tick entered an exception handler"
  assert cm.is_running is False


def test_carrot_navi_route_curvature_speed_uses_physical_formula():
  # Reach the curvature->speed branch (len(resampled_points) >= 9) so the
  # replacement of the never-defined V_CURVE_LOOKUP_* table is executed.
  cm = _instantiate()
  cm.params.get_bool = lambda key, default=False: False  # is_onroad = True
  cm.navi_points_active = True
  cm.navd_active = True
  cm.navi_points = [(0.0, 0.0), (0.001, 0.0), (0.002, 0.0)]

  class SMStub:
    def __getitem__(self, key):
      return SimpleNamespace(vEgo=10.0)

  cm.sm = SMStub()
  serv = cm.carrot_serv
  serv.active_carrot = 2
  serv.vpPosPointLon = 0.0
  serv.vpPosPointLat = 0.0
  serv.bearing = 0.0
  serv.nRoadLimitSpeed = 50.0
  serv.autoNaviSpeedDecelRate = 1.0
  serv.autoNaviSpeedCtrlEnd = 0.0

  class FakeLineString:
    def __init__(self, coords):
      pass

    @property
    def length(self):
      return 100.0

    def interpolate(self, distance):
      return SimpleNamespace(x=float(distance), y=0.0)

  with ExitStack() as stack:
    stack.enter_context(patch(f"{MODULE}.SHAPELY_AVAILABLE", True))
    stack.enter_context(patch(f"{MODULE}.LineString", FakeLineString, create=True))
    stack.enter_context(patch(f"{MODULE}.get_path_after_distance", return_value=(["p0", "p1"], 0, (0.0, 0.0))))
    stack.enter_context(patch(f"{MODULE}.gps_to_relative_xy", return_value=[(0.0, 0.0), (1.0, 0.0)]))
    stack.enter_context(patch(f"{MODULE}.calculate_curvature", return_value=0.001))
    points, distances, out_speed = cm.carrot_navi_route()

  assert len(points) >= 9  # curvature loop actually ran
  assert 0 < out_speed <= carrot_man_module.NO_LIMIT_KPH


def test_no_undefined_names_in_carrot_modules():
  # Pure-AST + symbol check (no external binary, CI-safe): stripped symbols
  # must stay gone and the new imports/constants must stay bound.
  legacy_names = ("queue_carrot_exception_tmux_send", "V_CURVE_LOOKUP_BP", "V_CRUVE_LOOKUP_VALS")
  legacy_tokens = ("put_nonblocking", "NetworkAddress", "TrafficLight")

  for module in (carrot_man_module, carrot_serv_module):
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    loaded_names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    constants = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for name in legacy_names:
      assert name not in loaded_names, f"{module.__name__} still references {name}"
    for token in legacy_tokens:
      assert token not in attributes and token not in constants, f"{module.__name__} still uses {token}"

  for name in ("traceback", "Any", "Dict", "Optional", "TARGET_LAT_ACCEL", "NO_LIMIT_KPH", "NAVI_HTTP_PORT"):
    assert hasattr(carrot_man_module, name), f"carrot_man is missing {name}"

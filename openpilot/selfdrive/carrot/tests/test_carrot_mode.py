"""Arbitration mode tests: 覆盖 / 回落 / 过渡稳定 (spec carrot-onroad-display)."""
import time

from openpilot.common.params import UnknownKeyName
from openpilot.selfdrive.carrot.carrot_man_input import get_carrot_man
from openpilot.selfdrive.carrot.carrot_mode import CarrotMode


class FakeCarrotMan:
  def __init__(self, desired_speed=80):
    self.desiredSpeed = desired_speed


class FakeSM:
  def __init__(self, received=True, desired_speed=80, age=0.0):
    self.seen = {"carrotMan": received}
    self.alive = {"carrotMan": received}
    self.valid = {"carrotMan": received}
    self.recv_time = {"carrotMan": time.monotonic() - age}
    self._cm = FakeCarrotMan(desired_speed)

  def __getitem__(self, key):
    return self._cm


def fake_sm(received=True, desired_speed=80, age=0.0):
  return FakeSM(received, desired_speed, age)


def test_guard_rejects_unseen_stale_and_bad_speed():
  assert get_carrot_man(fake_sm(received=False)) is None
  assert get_carrot_man(fake_sm(age=2.0)) is None
  assert get_carrot_man(fake_sm(desired_speed=0)) is None
  assert get_carrot_man(fake_sm(desired_speed=251)) is None
  assert get_carrot_man(fake_sm()) is not None


def test_mode_enters_after_continuous_valid_window():
  # 覆盖: mode (and hence the UI override) turns on only after N seconds of
  # continuous guard validity - a freshly started 搭子 does not flip the display.
  mode = CarrotMode(enter_s=3.0, exit_s=10.0)
  t0 = 100.0
  assert mode.update(True, now=t0) is False
  assert mode.update(True, now=t0 + 2.9) is False
  assert mode.update(True, now=t0 + 3.0) is True


def test_mode_ignores_brief_valid_blips_during_entry():
  mode = CarrotMode(enter_s=3.0, exit_s=10.0)
  t0 = 100.0
  mode.update(True, now=t0)
  mode.update(False, now=t0 + 1.0)  # 搭子 drops mid-entry
  assert mode.update(True, now=t0 + 2.0) is False  # entry window restarts
  assert mode.update(True, now=t0 + 5.0) is True


def test_mode_holds_through_short_disconnects():
  # 过渡稳定: 2s 内连断不切换 - a navRelay blip inside the exit grace keeps
  # the authority mode (and display source) unchanged.
  mode = CarrotMode(enter_s=3.0, exit_s=10.0)
  t0 = 100.0
  mode.update(True, now=t0)
  assert mode.update(True, now=t0 + 4.0) is True

  assert mode.update(False, now=t0 + 5.0) is True   # stale, still in grace
  assert mode.update(False, now=t0 + 8.0) is True   # 3s stale, still held
  assert mode.update(True, now=t0 + 9.0) is True    # recovered - no exit ever happened
  assert mode.update(False, now=t0 + 10.0) is True
  assert mode.update(False, now=t0 + 19.9) is True
  assert mode.update(False, now=t0 + 20.0) is False  # 10s continuous stale → 回落


def test_mode_exit_recovers_entry_hysteresis():
  # 行驶中切换: after a full exit, re-entry requires the full N-second window again.
  mode = CarrotMode(enter_s=3.0, exit_s=10.0)
  t0 = 100.0
  mode.update(True, now=t0)
  mode.update(True, now=t0 + 3.0)
  mode.update(False, now=t0 + 20.0)
  assert mode.update(False, now=t0 + 30.0) is False  # 10s continuous stale → exit
  assert mode.update(True, now=t0 + 40.0) is False   # entry window restarts
  assert mode.update(True, now=t0 + 43.0) is True


def test_mode_seconds_from_params_defaults():
  from openpilot.selfdrive.carrot.carrot_mode import carrot_mode_seconds_from_params

  class FakeParams:
    def get(self, key, return_default=False, **kwargs):
      raise UnknownKeyName(key)

    def get_param_path(self, key):
      raise OSError("no file")

  enter_s, exit_s = carrot_mode_seconds_from_params(FakeParams())
  assert enter_s == 3.0
  assert exit_s == 10.0

"""
Assist-less lane change - vendor port (op_byd "rick - assist-less lane change",
controlsd.py:691-703; design sunnypilot docs/byd-lane-change.md §四, ported
from sunnypilot 1b0713ac11 T3).

When LaneChangeAssistSpeed is 0 (turn-signal lane change assist disabled), a
blinker plus the driver holding the wheel in the same direction releases
lateral control entirely for the duration of the lane change: the driver steers
manually and openpilot never fights the EPS LKAS session mid-maneuver. The
state latches until BOTH blinkers are off. Sign convention note: BYD
SteerDriverTorque was measured left=positive on archived routes
(docs/byd-lane-change.md §四.1), same as openpilot, so the vendor's
torque-direction test is ported verbatim.

Carrot adaptation: sunnypilot refreshes the param from controlsd's
get_params_sp hook and selfdrived's params_thread; this fork has neither at
the same call sites, so the instance self-refreshes every 100 update() calls
(1 s at 100 Hz, the desire_helper precedent) - both the controlsd and the
selfdrived copy then behave identically without host wiring.
"""
from opendbc.car.structs import car

from openpilot.common.params import Params
from openpilot.common.raw_params import get_int_param


class NullAssistLessLaneChange:
  """Stock default for every non-BYD brand: never releases lateral."""
  def update(self, CS) -> bool:
    return False


def get_assist_less_lane_change(brand: str):
  """Brand factory wired at the controlsd/selfdrived call sites. The vendor
  yield mode is tied to BYD's torque sign calibration and its
  dp_lat_lane_change_assist_speed semantics, so only BYD gets the real latch;
  stock brands get the no-op and are unaffected even if the param is set."""
  if brand == "byd":
    return AssistLessLaneChange()
  return NullAssistLessLaneChange()


class AssistLessLaneChange:
  def __init__(self):
    self.params = Params()

    self.assist_disabled = False
    self.active = False
    self.frame = 0

    self.read_params()

  def read_params(self) -> None:
    # get_int_param works even before the device params_pyx.so registers the key
    # (falls back to the raw /data/params file); unset defaults to 1 = stock assist mode
    self.assist_disabled = get_int_param(self.params, "LaneChangeAssistSpeed", 1) == 0

  def update(self, CS: car.CarState) -> bool:
    self.frame += 1
    if self.frame % 100 == 0:
      self.read_params()

    if not self.assist_disabled:
      # assisted mode owns the blinker+steer case via the desire_helper state machine
      self.active = False
      return False

    # de-activate
    if not CS.leftBlinker and not CS.rightBlinker:
      self.active = False

    # activate: wheel held in the blinker direction (left = positive torque)
    if not self.active and CS.steeringPressed and \
       ((CS.steeringTorque > 0 and CS.leftBlinker) or
        (CS.steeringTorque < 0 and CS.rightBlinker)):
      self.active = True

    return self.active

"""Carrot authority mode: hysteresis around the carrotMan staleness guard.

The raw guard (carrot_man_input.get_carrot_man) gates actual control use:
stale carrot data is never consumed. The authority *mode* - what the UI
display source and the SLA/SCC gating follow - additionally debounces with
N seconds to enter and M seconds to exit, so navRelay toggles and phone
lock screens don't flip the display or re-arm SLA mid-drive (spec
carrot-speed-control 仲裁滞回 / carrot-onroad-display 过渡期显示稳定).

Shared by UI (P1) and planner/controlsd (P3); every consumer holds its own
instance fed from its own carrotMan SubMaster view.
"""
import time

from openpilot.common.params import Params
from openpilot.common.raw_params import get_int_param
from openpilot.selfdrive.carrot.carrot_man_input import get_carrot_man

# Design Open Question 3 initial values: M at carrot_man's 10s UDP timeout
# level, N 2-3s; calibrated on the car in task 7.8.
DEFAULT_ENTER_S = 3.0
DEFAULT_EXIT_S = 10.0
PARAM_ENTER_S = "CarrotModeEnterSec"
PARAM_EXIT_S = "CarrotModeExitSec"


def carrot_mode_seconds_from_params(params: Params) -> tuple[float, float]:
  enter_s = float(get_int_param(params, PARAM_ENTER_S, int(DEFAULT_ENTER_S)))
  exit_s = float(get_int_param(params, PARAM_EXIT_S, int(DEFAULT_EXIT_S)))
  return max(0.0, enter_s), max(0.0, exit_s)


class CarrotMode:
  def __init__(self, enter_s: float = DEFAULT_ENTER_S, exit_s: float = DEFAULT_EXIT_S) -> None:
    self.enter_s = float(enter_s)
    self.exit_s = float(exit_s)
    self.active = False
    self._valid_since: float | None = None
    self._stale_since: float | None = None

  def update(self, valid: bool, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now

    if valid:
      self._stale_since = None
      if self._valid_since is None:
        self._valid_since = now
      if not self.active and now - self._valid_since >= self.enter_s:
        self.active = True
    else:
      self._valid_since = None
      if self._stale_since is None:
        self._stale_since = now
      if self.active and now - self._stale_since >= self.exit_s:
        self.active = False

    return self.active

  def update_from_sm(self, sm) -> bool:
    return self.update(get_carrot_man(sm) is not None)

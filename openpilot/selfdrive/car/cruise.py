import math
import numpy as np

from opendbc.car.structs import car
from openpilot.common.constants import CV
from openpilot.common.raw_params import get_int_param
from openpilot.sunnypilot.selfdrive.car.cruise_ext import VCruiseHelperSP


# WARNING: this value was determined based on the model's training distribution,
#          model predictions above this speed can be unpredictable
# V_CRUISE's are in kph
V_CRUISE_MIN = 8
V_CRUISE_MAX = 145
V_CRUISE_UNSET = 255
V_CRUISE_INITIAL = 40
IMPERIAL_INCREMENT = round(CV.MPH_TO_KPH, 1)  # round here to avoid rounding errors incrementing set speed

ButtonEvent = car.CarState.ButtonEvent
ButtonType = car.CarState.ButtonEvent.Type
CRUISE_LONG_PRESS = 50
CRUISE_NEAREST_FUNC = {
  ButtonType.accelCruise: math.ceil,
  ButtonType.decelCruise: math.floor,
}
CRUISE_INTERVAL_SIGN = {
  ButtonType.accelCruise: +1,
  ButtonType.decelCruise: -1,
}


class VCruiseHelper(VCruiseHelperSP):
  def __init__(self, CP, CP_SP):
    VCruiseHelperSP.__init__(self, CP, CP_SP)
    self.CP = CP
    self.v_cruise_kph = V_CRUISE_UNSET
    self.v_cruise_cluster_kph = V_CRUISE_UNSET
    self.v_cruise_kph_last = 0
    self.button_timers = {ButtonType.decelCruise: 0, ButtonType.accelCruise: 0}
    self.button_change_states = {btn: {"standstill": False, "enabled": False} for btn in self.button_timers}
    # carrot 巡航预设跟随道路限速（card.py 每帧从 carrotMan 喂 road limit）
    self.carrot_road_limit_kph = 0
    self.carrot_road_speed_limit_offset = get_int_param(self.params, "AutoRoadSpeedLimitOffset", -1)
    self.carrot_navi_speed_safety_factor = get_int_param(self.params, "AutoNaviSpeedSafetyFactor", 105) * 0.01

  @property
  def v_cruise_initialized(self):
    return self.v_cruise_kph != V_CRUISE_UNSET

  def update_v_cruise(self, CS, enabled, is_metric):
    self.v_cruise_kph_last = self.v_cruise_kph

    self.get_minimum_set_speed(is_metric)

    _enabled = self.update_enabled_state(CS, enabled)

    if CS.cruiseState.available:
      if not self.CP.pcmCruise or (not self.CP_SP.pcmCruiseSpeed and _enabled):
        # if stock cruise is completely disabled, then we can use our own set speed logic
        self._update_v_cruise_non_pcm(CS, _enabled, is_metric)
        self.update_speed_limit_assist_v_cruise_non_pcm()
        self.v_cruise_cluster_kph = self.v_cruise_kph
      else:
        if self.CP.brand == "byd":
          # BYD stalk-owned target (Song Plus DM-i, real-car bug 2026-10-05:
          # Experimental + AlphaLong never accelerated, speed pinned at ~35).
          # BYD is pcmCruise=True (byd interface) and the stock branch below
          # maps SetSpeed==0 to V_CRUISE_UNSET - but BYD CarState emits no
          # buttonEvents (byd carstate only caches PCM_BUTTONS for the RES
          # spoof), so nothing can ever raise that unset number and the long
          # plan never commands positive accel. The number the driver CAN
          # change is the stalk's radar SetSpeed, which the port mirrors into
          # cruiseState.speed/speedCluster (no artificial 30 km/h floor), so
          # sync it directly gated by brand. Zero-hold: main-on frames can
          # carry SetSpeed==0 for the first 1-2 s of a session (byd
          # carcontroller, routes 31/2d) - keep the last real target through
          # the blip instead of commanding a deceleration to zero.
          if CS.cruiseState.speed > 0:
            self.v_cruise_kph = CS.cruiseState.speed * CV.MS_TO_KPH
            self.v_cruise_cluster_kph = CS.cruiseState.speedCluster * CV.MS_TO_KPH
        else:
          self.v_cruise_kph = CS.cruiseState.speed * CV.MS_TO_KPH
          self.v_cruise_cluster_kph = CS.cruiseState.speedCluster * CV.MS_TO_KPH
          if CS.cruiseState.speed == 0:
            self.v_cruise_kph = V_CRUISE_UNSET
            self.v_cruise_cluster_kph = V_CRUISE_UNSET
          elif CS.cruiseState.speed == -1:
            self.v_cruise_kph = -1
            self.v_cruise_cluster_kph = -1
    else:
      self.v_cruise_kph = V_CRUISE_UNSET
      self.v_cruise_cluster_kph = V_CRUISE_UNSET

    if not self.CP.pcmCruise or not self.CP_SP.pcmCruiseSpeed:
      self.update_button_timers(CS, enabled)

  def _update_v_cruise_non_pcm(self, CS, enabled, is_metric):
    # handle button presses. TODO: this should be in state_control, but a decelCruise press
    # would have the effect of both enabling and changing speed is checked after the state transition
    if not enabled:
      return

    long_press = False
    button_type = None

    v_cruise_delta = 1. if is_metric else IMPERIAL_INCREMENT

    for b in CS.buttonEvents:
      if b.type.raw in self.button_timers and not b.pressed:
        if self.button_timers[b.type.raw] > CRUISE_LONG_PRESS:
          return  # end long press
        button_type = b.type.raw
        break
    else:
      for k, timer in self.button_timers.items():
        if timer and timer % CRUISE_LONG_PRESS == 0:
          button_type = k
          long_press = True
          break

    if button_type is None:
      return

    # Don't adjust speed when pressing resume to exit standstill
    cruise_standstill = self.button_change_states[button_type]["standstill"] or CS.cruiseState.standstill
    if button_type == ButtonType.accelCruise and cruise_standstill:
      return

    # Don't adjust speed if we've enabled since the button was depressed (some ports enable on rising edge)
    if not self.button_change_states[button_type]["enabled"]:
      return

    # Speed Limit Assist for Non PCM long cars.
    # True: Disallow set speed changes when user confirmed the target set speed during preActive state
    # False: Allow set speed changes as SLA is not requesting user confirmation
    if self.update_speed_limit_assist_pre_active_confirmed(button_type):
      return

    long_press, v_cruise_delta = VCruiseHelperSP.update_v_cruise_delta(self, long_press, v_cruise_delta)
    if long_press and self.v_cruise_kph % v_cruise_delta != 0:  # partial interval
      self.v_cruise_kph = CRUISE_NEAREST_FUNC[button_type](self.v_cruise_kph / v_cruise_delta) * v_cruise_delta
    else:
      self.v_cruise_kph += v_cruise_delta * CRUISE_INTERVAL_SIGN[button_type]

    # If set is pressed while overriding, clip cruise speed to minimum of vEgo
    if CS.gasPressed and button_type in (ButtonType.decelCruise, ButtonType.setCruise):
      self.v_cruise_kph = max(self.v_cruise_kph, CS.vEgo * CV.MS_TO_KPH)

    self.v_cruise_kph = np.clip(round(self.v_cruise_kph, 1), self.v_cruise_min, V_CRUISE_MAX)

  def update_button_timers(self, CS, enabled):
    # increment timer for buttons still pressed
    for k in self.button_timers:
      if self.button_timers[k] > 0:
        self.button_timers[k] += 1

    for b in CS.buttonEvents:
      if b.type.raw in self.button_timers:
        # Start/end timer and store current state on change of button pressed
        self.button_timers[b.type.raw] = 1 if b.pressed else 0
        self.button_change_states[b.type.raw] = {"standstill": CS.cruiseState.standstill, "enabled": enabled}

  def initialize_v_cruise(self, CS, initial_speed: int) -> None:
    # initializing is handled by the PCM
    if self.CP.pcmCruise:
      return

    if any(b.type in (ButtonType.accelCruise, ButtonType.resumeCruise) for b in CS.buttonEvents) and self.v_cruise_initialized:
      self.v_cruise_kph = self.v_cruise_kph_last
    else:
      # carrot 权威期预设跟随道路限速（×安全系数 / +偏移，与 carrot 行为一致）；
      # road limit 由 card.py 每帧从 carrotMan 喂入
      if self.carrot_road_limit_kph > 0:
        if self.carrot_road_speed_limit_offset < 0:
          initial_speed = int(round(self.carrot_road_limit_kph * self.carrot_navi_speed_safety_factor))
        else:
          initial_speed = int(round(self.carrot_road_limit_kph + self.carrot_road_speed_limit_offset))
      self.v_cruise_kph = int(round(np.clip(max(CS.vEgo * CV.MS_TO_KPH, initial_speed), V_CRUISE_INITIAL, V_CRUISE_MAX)))

    self.v_cruise_cluster_kph = self.v_cruise_kph

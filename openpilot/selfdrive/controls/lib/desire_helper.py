from openpilot.cereal import log, custom
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot.selfdrive.controls.lib.auto_lane_change import AutoLaneChangeController, AutoLaneChangeMode
from openpilot.sunnypilot.selfdrive.controls.lib.lane_turn_desire import LaneTurnController

LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection
TurnDirection = custom.ModelDataV2SP.TurnDirection

LANE_CHANGE_SPEED_MIN = 20 * CV.MPH_TO_MS
LANE_CHANGE_TIME_MAX = 10.
LANE_CHANGE_START_TIME = 0.5

TURN_DESIRES = {
  TurnDirection.none: log.Desire.none,
  TurnDirection.turnLeft: log.Desire.turnLeft,
  TurnDirection.turnRight: log.Desire.turnRight,
}

class DesireHelper:
  def __init__(self):
    self.params = Params()
    self.frame = 0
    self.lane_change_state = LaneChangeState.off
    self.lane_change_direction = LaneChangeDirection.none
    self.lane_change_timer = 0.0
    self.prev_one_blinker = False
    self.desire = log.Desire.none
    self.alc = AutoLaneChangeController(self)
    self.lane_turn_controller = LaneTurnController(self)
    self.lane_turn_direction = TurnDirection.none
    # state slot for the brand clear-window hooks (see _update_clear_window);
    # unused by the stock no-op implementations
    self.lane_change_clear_time = 0.0
    # lane-change minimum speed floor; stock constant, brand subclasses may
    # replace it with a param-driven value (see desire_helper_byd)
    self.lane_change_speed_floor_ms = LANE_CHANGE_SPEED_MIN
    # carrot ATC 转弯区间（activeCarrot>1 ∧ 0<xDistToTurn<100）：冻结变道发起，
    # 由 modeld 每帧从 carrotMan 喂入（见 carrot_cruise.CarrotCruise.atc_active）
    self.carrot_atc_active = False
    # 导航变道许可闸门（拨杆触发的导航引导变道）：由 modeld 每帧从
    # navi_lane_change_gate.NaviLaneChangeGate 喂入；fail-open——闸门未启用或
    # carrotmate 掉线时恒为 True，行为与纯拨杆 ALC 完全一致
    self.navi_lc_allowed = True

  # ── brand adaptation hooks - stock defaults are behavior-neutral no-ops;
  #    desire_helper_byd.BydDesireHelper overrides them with the vendor port ──
  def _update_clear_window(self, carstate):
    pass

  def _clear_window_ok(self) -> bool:
    return True

  def _mid_maneuver_abort(self, carstate) -> bool:
    return False

  def _read_params(self):
    # brand subclass extension point, refreshed every 100 update() calls
    pass

  @staticmethod
  def get_lane_change_direction(CS):
    return LaneChangeDirection.left if CS.leftBlinker else LaneChangeDirection.right

  def update(self, carstate, lateral_active, lane_change_prob, left_edge_detected=False, right_edge_detected=False):
    self.frame += 1
    if self.frame % 100 == 0:
      self._read_params()
    self.alc.update_params()
    self.lane_turn_controller.update_params()
    v_ego = carstate.vEgo
    one_blinker = carstate.leftBlinker != carstate.rightBlinker
    below_lane_change_speed = v_ego < self.lane_change_speed_floor_ms

    # Lane turn controller update
    self.lane_turn_controller.update_lane_turn(blindspot_left=carstate.leftBlindspot, blindspot_right=carstate.rightBlindspot,
                                               left_blinker=carstate.leftBlinker, right_blinker=carstate.rightBlinker, v_ego=v_ego)
    self.lane_turn_direction = self.lane_turn_controller.get_turn_direction()

    if not lateral_active or self.lane_change_timer > LANE_CHANGE_TIME_MAX or self.alc.lane_change_set_timer == AutoLaneChangeMode.OFF:
      self.lane_change_state = LaneChangeState.off
      self.lane_change_direction = LaneChangeDirection.none
      self.lane_change_timer = 0.0
    else:
      if self.lane_change_state == LaneChangeState.off and one_blinker and not self.prev_one_blinker and not below_lane_change_speed:
        self.lane_change_state = LaneChangeState.preLaneChange
        self.lane_change_timer = 0.0
        self.lane_change_clear_time = 0.0
        # Initialize lane change direction to prevent UI alert flicker
        self.lane_change_direction = self.get_lane_change_direction(carstate)

      elif self.lane_change_state == LaneChangeState.preLaneChange:
        # Update lane change direction
        self.lane_change_direction = self.get_lane_change_direction(carstate)

        torque_applied = carstate.steeringPressed and \
                         ((carstate.steeringTorque > 0 and self.lane_change_direction == LaneChangeDirection.left) or
                          (carstate.steeringTorque < 0 and self.lane_change_direction == LaneChangeDirection.right))

        blindspot_detected = ((carstate.leftBlindspot and self.lane_change_direction == LaneChangeDirection.left) or
                              (carstate.rightBlindspot and self.lane_change_direction == LaneChangeDirection.right))

        edge_detected = ((left_edge_detected and self.lane_change_direction == LaneChangeDirection.left) or
                         (right_edge_detected and self.lane_change_direction == LaneChangeDirection.right))

        self.alc.update_lane_change(blindspot_detected, edge_detected, carstate.brakePressed, torque_applied)

        # brand clear-window hook (stock no-op; BYD accumulates same-side
        # blindspot clearance - see desire_helper_byd)
        self._update_clear_window(carstate)

        if not one_blinker or below_lane_change_speed:
          self.lane_change_state = LaneChangeState.off
          self.lane_change_direction = LaneChangeDirection.none
          self.lane_change_timer = 0.0
        # brand clear-window gate (stock _clear_window_ok() is always True;
        # BYD requires fresh rear-traffic clearance before any start path)
        elif (torque_applied or self.alc.auto_lane_change_allowed) and not self.alc.lane_change_blocked \
          and not self.carrot_atc_active and self.navi_lc_allowed and self._clear_window_ok():
          self.lane_change_state = LaneChangeState.laneChangeStarting
          self.lane_change_timer = 0.0
          self.lane_change_clear_time = 0.0

      elif self.lane_change_state == LaneChangeState.laneChangeStarting:
        # brand mid-maneuver abort hook (stock returns False; BYD falls back
        # to preLaneChange KEEPING the direction on a fresh same-side BSD hit,
        # see desire_helper_byd - laneChangeFinishing is never aborted)
        if self._mid_maneuver_abort(carstate):
          self.lane_change_state = LaneChangeState.preLaneChange
          self.lane_change_direction = self.get_lane_change_direction(carstate)
          self.lane_change_timer = 0.0
          self.lane_change_clear_time = 0.0
        else:
          self.lane_change_timer += DT_MDL
          if lane_change_prob < 0.02 and self.lane_change_timer >= LANE_CHANGE_START_TIME:
            self.lane_change_timer = 0.0
            if one_blinker:
              self.lane_change_state = LaneChangeState.preLaneChange
              self.lane_change_direction = self.get_lane_change_direction(carstate)
            else:
              self.lane_change_state = LaneChangeState.off
              self.lane_change_direction = LaneChangeDirection.none

    self.prev_one_blinker = one_blinker and lateral_active

    if self.lane_turn_direction != TurnDirection.none:
      self.desire = TURN_DESIRES[self.lane_turn_direction]
    else:
      self.desire = log.Desire.none
      if self.lane_change_state == LaneChangeState.laneChangeStarting:
        if self.lane_change_direction == LaneChangeDirection.left:
          self.desire = log.Desire.laneChangeLeft
        elif self.lane_change_direction == LaneChangeDirection.right:
          self.desire = log.Desire.laneChangeRight

    self.alc.update_state()


def get_desire_helper(brand: str) -> DesireHelper:
  """Brand factory for the desire machine. Stock brands get the base class;
  BYD gets BydDesireHelper (vendor lane-change port). Lazy import keeps the
  stock DesireHelper module free of a top-level BYD dependency."""
  if brand == "byd":
    from openpilot.selfdrive.controls.lib.desire_helper_byd import BydDesireHelper
    return BydDesireHelper()
  return DesireHelper()

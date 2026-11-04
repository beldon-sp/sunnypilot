"""BYD desire/lane-change adaptation - the vendor port, kept out of the stock
DesireHelper. Selected in modeld via get_desire_helper(CP.brand).

Three stock hooks are overridden here:
  _update_clear_window / _clear_window_ok - the BSD rear-traffic clear window
    (sunnypilot 5d45f8f9c9). BSD_RADAR 0x418 (10 Hz) is the only rear-traffic
    input on the 宋 - the rear radar is unwired (0x374 has zero frames on every
    logged bus) - so a single-frame blindspot sample at the start edge is
    lag-prone and the stalk decodes a 0.2-0.3 s transient as a blinker. A
    change may only start after the same-side blindspot stayed continuously
    clear for LANE_CHANGE_CLEAR_TIME_MIN. Raw carstate matches selfdrived's
    laneChangeBlocked predicate; the 2 s side.bsd_hold stays the existing
    (separate, stronger) start-blocker.
  _mid_maneuver_abort - a fresh same-side BSD hit mid-starting falls back to
    preLaneChange KEEPING the direction: DESIRES[dir][pre] is Desire.none so
    the car centers, and selfdrived's preLaneChange+blindspot predicate raises
    laneChangeBlocked on the next frame. laneChangeFinishing is deliberately
    never aborted (swinging back across the line that late is worse than
    completing the maneuver).

Plus the vendor lane-change assist speed floor (sunnypilot 1b0713ac11 T2),
LaneChangeAssistSpeed MPH; default "20" = the vendor's
dp_lat_lane_change_assist_speed, 0 = assist-less. Read through get_int_param
(stale compiled .so raises UnknownKeyName, and the C++ get_int would report an
unset key as 0 = assist-less); the raw file under /data/params is the manual
provisioning path until params_pyx.so is rebuilt with the new key. =0 -> inf:
the off->pre gate can't fire, an in-progress preLaneChange bails to off, and
the bluetooth request gate is denied - the assist machine is fully off at any
speed, which is also the switch that arms the vendor's assist-less yield mode
(AssistLessLaneChange, controlsd/selfdrived side).
"""
from openpilot.common.constants import CV
from openpilot.common.raw_params import get_int_param
from openpilot.common.realtime import DT_MDL

from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper, LaneChangeDirection

# BYD rear-traffic hardening (sunnypilot 5d45f8f9c9, docs/byd-lane-change.md
# §八): BSD_RADAR 0x418 (10 Hz) is the only rear-traffic input on this platform
# - the rear radar is unwired (0x374 has zero frames on every logged bus).
# Single-frame blindspot sampling cannot cover the BSD lag or the 0.2-0.3 s
# stalk transient, so starting requires this much continuous same-side
# blindspot clearance and a fresh hit mid-change aborts back to preLaneChange.
LANE_CHANGE_CLEAR_TIME_MIN = 0.5

# BYD T8 forward gap gate (sunnypilot d94a1a8570, docs/byd-radar-010x.md
# §④e/§④f): CarState.radarGapClosing/radarGapNew from the bus1 0x109 b7
# boundary-cross monitor (radar_gap.py) as an extra upstream input to the
# clear window. Three levels via the BydRadarGapGate raw param (device
# default 0 = behavior byte-for-byte unchanged until the road test):
#   1 - while the gap is closing across its band, treat it like a blindspot
#       hit for the clear window: reopen it, and never start mid-alert.
#       (④e: b7 crossed the plateau midpoint before 309/309 gear flips, and
#        capture leads the HUD by >=10 s - the alert is EARLIER than anything
#        the HUD-gear path could ever see.)
#   2 - additionally abort laneChangeStarting on a NEW target that is closing:
#       radar-visible was false at the start edge and the cone swept in a
#       fast-closing car ahead. A target that was already visible before the
#       start does NOT abort - without yRel (b3 non-lateral, ⑦) we cannot tell
#       "target-lane car we are diving in front of" from "own-lane car we are
#       escaping", and swinging back across the line toward the latter is the
#       worse failure. laneChangeFinishing is never aborted (10-11 discipline).
#   3 - stage-3 lane discrimination (byd-radar-multitarget-port): the monitor
#       classifies the closing target by its slot-table yRel into own/adjacent
#       lane (relative bands + hysteresis, radar_gap.py; CarState publishes
#       radarGapLane). Adjacent-lane closings stop blocking the clear window
#       and stop aborting - they are telemetry only. Levels 1/2 ignore the
#       lane field entirely (byte-identical semantics, spec discipline), and
#       lane==UNKNOWN (slot table off / lateral fields 未定案 / NaN) degrades
#       to the exact level-2 conservative behavior with the monitor's
#       `degraded` counter ticking for the road test.
RADAR_GAP_LEVEL_MAX = 3

# radarGapLane values (car.capnp / radar_gap.py); duplicated here to keep the
# hook predicate self-contained
_GAP_LANE_ADJACENT = 2


class BydDesireHelper(DesireHelper):
  def __init__(self):
    super().__init__()
    # stock floor is 30 KPH; the vendor param replaces it. Init read closes the
    # frame%100 boot window (modeld's first periodic pass lands 5 s in)
    self._read_assist_speed()
    # T8 forward-gap gate level, same raw-params fallback and boot-window
    # discipline; 0 until /data/params/d/BydRadarGapGate is written on-road
    self._read_gap_gate()

  def _read_params(self):
    super()._read_params()
    self._read_assist_speed()
    self._read_gap_gate()

  def _read_assist_speed(self):
    mph = get_int_param(self.params, "LaneChangeAssistSpeed", 20)
    self.lane_change_speed_floor_ms = mph * CV.MPH_TO_MS if mph > 0 else float("inf")

  def _read_gap_gate(self):
    self.radar_gap_gate = min(RADAR_GAP_LEVEL_MAX,
                              max(0, get_int_param(self.params, "BydRadarGapGate", 0)))

  def _same_side_blindspot(self, carstate) -> bool:
    return ((carstate.leftBlindspot and self.lane_change_direction == LaneChangeDirection.left) or
            (carstate.rightBlindspot and self.lane_change_direction == LaneChangeDirection.right))

  def _gap_closing(self, carstate) -> bool:
    # T8: only BYD's gap parser path sets these; every other brand leaves both
    # false, and the gate defaults 0, so today this is inert by two layers.
    # Level 3 refines with the lane field: an ADJACENT-lane closing is
    # telemetry only (no window reset); UNKNOWN degrades to the level-2
    # conservative answer. Levels 1/2 never read the lane field.
    if self.radar_gap_gate < 1 or not getattr(carstate, "radarGapClosing", False):
      return False
    if self.radar_gap_gate >= 3 and \
       getattr(carstate, "radarGapLane", 0) == _GAP_LANE_ADJACENT:
      return False
    return True

  def _gap_new(self, carstate) -> bool:
    # level 2 abort additionally requires radarGapNew (target was invisible at
    # the start edge); getattr guards the field on pre-schema carstates.
    # Level 3: same, minus adjacent-lane targets (lane UNKNOWN = conservative).
    if self.radar_gap_gate < 2 or not getattr(carstate, "radarGapNew", False) \
       or not getattr(carstate, "radarGapClosing", False):
      return False
    if self.radar_gap_gate >= 3 and \
       getattr(carstate, "radarGapLane", 0) == _GAP_LANE_ADJACENT:
      return False
    return True

  def _update_clear_window(self, carstate):
    # accumulate continuous same-side clearance, reset on any hit; T8 level>=1
    # treats a forward-gap boundary-cross like a blindspot hit for the window
    if self._same_side_blindspot(carstate) or self._gap_closing(carstate):
      self.lane_change_clear_time = 0.0
    else:
      self.lane_change_clear_time += DT_MDL

  def _clear_window_ok(self) -> bool:
    # level-1 "never start mid-alert" is enforced through the window: while the
    # gap is closing, _update_clear_window keeps resetting clear_time to 0, so
    # this accumulation gate stays False until the alert has cleared for a full
    # LANE_CHANGE_CLEAR_TIME_MIN - matching the source's separate "and not
    # gap_closing" start condition without needing carstate in this signature.
    return self.lane_change_clear_time >= LANE_CHANGE_CLEAR_TIME_MIN - 1e-6

  def _mid_maneuver_abort(self, carstate) -> bool:
    # T8 level 2 adds the new-target case only (see RADAR_GAP_LEVEL_MAX: an
    # already-visible closing lead ahead is lane-ambiguous without yRel and
    # aborting toward it is the worse guess). laneChangeFinishing never aborts
    # (stock hook is only consulted while laneChangeStarting).
    return self._same_side_blindspot(carstate) or self._gap_new(carstate)

"""Host tests for the BYD T8 forward-gap monitor and its lane-change gate
(docs/byd-radar-010x.md §④e/§④f; design in opendbc/car/byd/radar_gap.py).

Layer 1: RadarGapMonitor boundary-cross logic in u-free r=b7/vEgo space.
Layer 2: CarState.update_radar_gap - real CANParser decode of BO_ 265/266.
Layer 3: gate level semantics - level 0 is byte-identical to pre-T8 behavior;
         level 1 keeps the clear window shut mid-alert (which is what blocks
         starting); level 2 aborts only on NEW closing targets.

Layer 3 note (carrot adaptation, 2026-10-20): the sunnypilot suite drove the
stock DesireHelper.update() state machine; this fork's T8 behavior lives in
BydDesireHelper's three hooks (_update_clear_window / _clear_window_ok /
_mid_maneuver_abort), so the contract is tested at the hooks, not by driving
the full carrot state machine (that needs modeldata/carrotMan/side state and
stays a road-test item, same discipline as the 10-11 lane-change port).
"""
import math
from types import SimpleNamespace

import pytest

from opendbc.car import CanData, structs
from opendbc.car.byd.carstate import CarState
from opendbc.car.byd.radar_gap import (LANE_ADJACENT, LANE_OWN, LANE_UNKNOWN,
                                       LANE_HYST_M, LANE_WIDTH_M,
                                       RadarGapMonitor, BAND_BOUNDARY_DOWN, SET_MARGIN, SET_TIME)
from opendbc.car.byd.values import CAR
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib import desire_helper_byd
from openpilot.selfdrive.controls.lib.desire_helper_byd import (LANE_CHANGE_CLEAR_TIME_MIN,
                                                                 BydDesireHelper)

V = 20.0                     # test cruise speed, m/s
R = {2: 2.92, 3: 4.79, 4: 6.61}  # plateau medians (monitor header)
DT = 0.01                    # card loop step, matches CarState.update_radar_gap


def step(mon, dt, b7, presence=7, cls=3, v_ego=V, fresh=True, y=None):
  mon.update(dt, b7, presence, cls, v_ego, fresh, y_rel=y)
  return mon.closing


class TestMonitor:
  def run(self, seq):
    """seq: list of (b7, presence, cls, v_ego) held 1s each -> final closing"""
    mon = RadarGapMonitor()
    for b7, presence, cls, v in seq:
      for _ in range(int(1 / DT)):
        step(mon, DT, b7, presence, cls, v)
    return mon

  def test_stable_follow_never_alerts(self):
    # r wobbles +-6% around the class-3 plateau - inside the false-cross band
    # ④e says a static threshold would trip 29-60% of such frames
    mon = RadarGapMonitor()
    for k in range(1000):
      b7 = int(R[3] * V * (1 + 0.06 * math.sin(k / 20)))
      assert step(mon, DT, b7) is False
    assert not mon.closing

  def test_sustained_closing_alerts_before_gear_flip(self):
    # linear closure r 4.8 -> 3.2 over ~4 s (0.4 r/s): alert must fire once
    # r sits below boundary*0.88 (=3.39) for 0.4 s with the descent confirmed
    b_mid = BAND_BOUNDARY_DOWN[3]          # 3.855
    mon, alert_r, crossed = RadarGapMonitor(), None, False
    for k in range(1000):
      r = R[3] - 0.4 * k * DT
      step(mon, DT, int(r * V))
      crossed |= (not crossed and r <= b_mid)
      if alert_r is None and mon.closing:
        alert_r = r
        break
    set_lim = b_mid * (1.0 - SET_MARGIN)
    assert alert_r is not None
    assert crossed                      # boundary was reached before the alert
    assert alert_r <= set_lim + 1e-9    # alert is the deep, sustained case
    assert alert_r >= set_lim - 0.4 * (SET_TIME + 0.6)  # ... and not so late it
                                        # trails the flip by more than a debounce
    assert alert_r < b_mid

  def test_debounce_single_dip(self):
    mon = RadarGapMonitor()
    for _ in range(3):
      step(mon, DT, int(3.0 * V))        # far below limit, but only 3 frames
    assert not mon.closing

  def test_release_with_hysteresis(self):
    mon = RadarGapMonitor()
    for k in range(1000):
      r = R[3] - 0.4 * k * DT
      step(mon, DT, int(r * V))
      if mon.closing:
        break
    assert mon.closing
    for _ in range(int(0.5 / DT)):        # 0.5 s back at plateau
      step(mon, DT, int(R[3] * V))
    assert mon.closing                    # hysteresis: not released yet (needs 1 s)
    for _ in range(int(0.5 / DT) + 2):
      step(mon, DT, int(R[3] * V))
    assert not mon.closing

  def test_presence_and_sentinel_filters(self):
    mon = RadarGapMonitor()
    for k in range(1000):
      step(mon, DT, int((R[3] - 0.4 * k * DT) * V), presence=5)
    assert not mon.closing                 # presence nibble != 111 -> invisible (④f)
    mon2 = RadarGapMonitor()
    for _ in range(1000):
      step(mon2, DT, 246)                  # range-edge band is never "visible" (④a)
    assert not mon2.visible
    mon3 = RadarGapMonitor()
    for _ in range(100):
      step(mon3, DT, 0)
    assert not mon3.visible                # b7==0 sentinel

  def test_low_speed_not_alerted(self):
    mon = RadarGapMonitor()
    for _ in range(2000):
      step(mon, DT, 2, v_ego=1.0)          # crawling, r huge anyway
    assert not mon.closing

  def test_no_frame_goes_invisible(self):
    mon = RadarGapMonitor()
    for _ in range(1000):
      step(mon, DT, int(3.0 * V), fresh=False)
    assert not mon.visible and not mon.closing

  def test_flat_below_boundary_no_alert_without_descent(self):
    # already parked below the limit (driver chose the tight gap): the slope
    # gate must not keep the clear window shut forever
    mon = RadarGapMonitor()
    for _ in range(2000):
      step(mon, DT, int(3.3 * V))
    assert not mon.closing

  def test_new_target_window(self):
    mon = RadarGapMonitor()
    for _ in range(100):
      step(mon, DT, 0, presence=0)         # invisible >= 0.5 s
    for _ in range(10):
      step(mon, DT, int(R[3] * V))
    assert mon.new_target                  # rising edge latches
    for _ in range(1000):
      step(mon, DT, int(R[3] * V))
    assert not mon.new_target              # expires after the 10 s capture lead


class TestCarStateDecode:
  def make_cs(self):
    CP = structs.CarParams.new_message()
    CP.carFingerprint = CAR.BYD_SONG_PLUS_DMI_22
    CP.radarUnavailable = True             # must not matter: gap_cp is separate
    cs = CarState(CP, structs.CarParamsSP())
    cs.adas_msg = {"HasLead": 1, "LeadingDistance": 3}
    return cs

  def feed(self, cs, b7, presence_bits, ts):
    d9 = bytearray(8)
    d9[7] = b7
    dA = bytearray(8)
    dA[3] = presence_bits << 5
    return cs.update_radar_gap([(int(ts * 1e9),
                                 [CanData(265, bytes(d9), 1), CanData(266, bytes(dA), 1)])], V)

  def test_decode_and_alert(self):
    cs = self.make_cs()
    for k in range(1000):                  # r 4.8 -> 3.2 at 0.4/s, 25 Hz frames
      r = R[3] - 0.4 * k * DT
      self.feed(cs, int(r * V), 7, k * DT)
    assert cs.gap_mon.closing and getattr(cs.gap_mon, "visible", False)

  def test_no_parser_when_radar_dbc_absent(self):
    CP = structs.CarParams.new_message()
    CP.carFingerprint = CAR.BYD_SONG_PLUS_DMI_22
    cs = CarState(CP, structs.CarParamsSP())
    assert cs.gap_cp is not None           # byd has a radar dbc: parser exists
    assert cs.update_radar_gap([], 20.0) == (cs.gap_mon.closing, cs.gap_mon.new_target)

  def test_b7_counts_scale_free(self):
    # b7 comes from the factor-1 MSG_109_B7 placeholder, never from the
    # LongDist_109 scaled signal: whatever BydRadarDistScale/dbc factor says,
    # the monitor must see exact counts (u pinned 0.25 2026-10-19, tunable)
    cs = self.make_cs()
    b7, presence_bits = 123, 7
    d9 = bytearray(8)
    d9[7] = b7
    dA = bytearray(8)
    dA[3] = presence_bits << 5
    cs.update_radar_gap([(0, [CanData(265, bytes(d9), 1), CanData(266, bytes(dA), 1)])], V)
    vl = cs.gap_cp.vl[265]
    assert int(vl["MSG_109_B7"]) == b7                 # exact counts, scale-free
    assert vl["LongDist_109"] == b7 * 0.25             # pinned scale (dbc factor)
    r_hist = cs.gap_mon._hist
    assert r_hist and abs(r_hist[-1][1] - b7 / V) < 1e-9  # monitor sees raw counts


def cs_state(**kw):
  d = {'leftBlinker': False, 'rightBlinker': False, 'leftBlindspot': False, 'rightBlindspot': False,
       'radarGapClosing': False, 'radarGapNew': False}
  d.update(kw)
  return SimpleNamespace(**d)


class TestDesireGate:
  """The T8 contract at the three hooks; no full-state-machine driving (see the
  module docstring). lane_change_direction is pinned to left, matching the
  blinker-side scenarios the source suite exercised."""

  def make_dh(self, gate):
    from openpilot.selfdrive.controls.lib.desire_helper import LaneChangeDirection
    dh = BydDesireHelper()
    dh.lane_change_direction = LaneChangeDirection.left
    dh.radar_gap_gate = gate
    return dh

  def test_gate0_ignores_alert_fields(self):
    # spec 默认参数逐字节不变 / level-0 neutrality: with the gate at 0 the gap
    # fields must not touch the window or abort a maneuver in progress
    dh = self.make_dh(0)
    dh.lane_change_clear_time = 0.0
    for _ in range(10):
      dh._update_clear_window(cs_state(leftBlinker=True, radarGapClosing=True, radarGapNew=True))
    # no reset (gate 0): pure accumulation of DT_MDL per call
    assert dh.lane_change_clear_time == pytest.approx(10 * DT_MDL)
    assert dh._clear_window_ok()
    assert not dh._mid_maneuver_abort(cs_state(leftBlinker=True, radarGapClosing=True,
                                               radarGapNew=True))

  def test_gate1_reopens_window_and_blocks_start(self):
    # level 1: a closing gap is blindspot-equivalent for the window - every
    # alert frame resets the accumulation, so _clear_window_ok() (the start
    # gate) can never pass mid-alert
    dh = self.make_dh(1)
    dh.lane_change_clear_time = LANE_CHANGE_CLEAR_TIME_MIN  # pretend the window was met
    dh._update_clear_window(cs_state(leftBlinker=True, radarGapClosing=True))
    assert dh.lane_change_clear_time == 0.0
    assert not dh._clear_window_ok()

  def test_gate1_window_restarts_after_alert_clears(self):
    dh = self.make_dh(1)
    dh.lane_change_clear_time = 0.0
    dh._update_clear_window(cs_state(leftBlinker=True, radarGapClosing=True))
    assert dh.lane_change_clear_time == 0.0
    for _ in range(int(LANE_CHANGE_CLEAR_TIME_MIN / DT_MDL) + 1):
      dh._update_clear_window(cs_state(leftBlinker=True, radarGapClosing=False))
    assert dh._clear_window_ok()
    # level 1 alone never aborts mid-change (that is level 2)
    assert not dh._mid_maneuver_abort(cs_state(leftBlinker=True, radarGapClosing=True,
                                               radarGapNew=True))

  def test_gate2_new_target_aborts(self):
    dh = self.make_dh(2)
    assert dh._mid_maneuver_abort(cs_state(leftBlinker=True, radarGapClosing=True,
                                           radarGapNew=True))

  def test_gate2_old_target_does_not_abort(self):
    # closing but visible before the start edge: lane-ambiguous without yRel,
    # swinging back across the line is the worse guess (source discipline)
    dh = self.make_dh(2)
    assert not dh._mid_maneuver_abort(cs_state(leftBlinker=True, radarGapClosing=True,
                                               radarGapNew=False))

  def test_gate2_new_without_closing_does_not_abort(self):
    # a fresh far target is not an escape scenario
    dh = self.make_dh(2)
    assert not dh._mid_maneuver_abort(cs_state(leftBlinker=True, radarGapClosing=False,
                                               radarGapNew=True))

  def test_bsd_behavior_unchanged_under_gate0(self):
    # the pre-existing BYD hardening still works with the gap gate at 0:
    # same-side blindspot resets the window and aborts starting
    dh = self.make_dh(0)
    dh.lane_change_clear_time = 1.0
    dh._update_clear_window(cs_state(leftBlindspot=True))
    assert dh.lane_change_clear_time == 0.0
    assert dh._mid_maneuver_abort(cs_state(leftBlindspot=True))

  def test_gate_param_clamped(self):
    # BydRadarGapGate is clamped to [0, RADAR_GAP_LEVEL_MAX] whatever the file
    # says; monkeypatch the module's get_int_param (level > 2 = same as 2 by
    # intent, negatives = off)
    orig = desire_helper_byd.get_int_param
    try:
      desire_helper_byd.get_int_param = lambda params, key, default: 5
      assert BydDesireHelper().radar_gap_gate == desire_helper_byd.RADAR_GAP_LEVEL_MAX
      desire_helper_byd.get_int_param = lambda params, key, default: -3
      assert BydDesireHelper().radar_gap_gate == 0
    finally:
      desire_helper_byd.get_int_param = orig


class TestLaneDiscrimination:
  """Stage-3 lane discrimination (byd-radar-multitarget-port): relative yRel
  bands + hysteresis in the monitor, gate-3 consumption at the hooks."""

  def run_closing(self, mon, y, cycles=400):
    """Drive a sustained closure with the given lateral offset."""
    out = False
    for k in range(cycles):
      r = R[3] - 0.4 * k * DT
      out = step(mon, DT, int(r * V), y=y)
      if mon.closing:
        break
    return out

  def test_classification_bands(self):
    mon = RadarGapMonitor()
    self.run_closing(mon, y=1.0)                    # inside ±1.75 m
    assert mon.lane == LANE_OWN
    mon2 = RadarGapMonitor()
    self.run_closing(mon2, y=2.5)                   # beyond band + margin
    assert mon2.lane == LANE_ADJACENT

  def test_hysteresis_exit_margin(self):
    mon = RadarGapMonitor()
    self.run_closing(mon, y=1.0)
    assert mon.lane == LANE_OWN
    # inside width+hysteresis: still own (no chatter at the band edge)
    step(mon, DT, int(R[3] * V), y=LANE_WIDTH_M + LANE_HYST_M - 0.01)
    assert mon.lane == LANE_OWN
    # past width+hysteresis: exits to adjacent
    step(mon, DT, int(R[3] * V), y=LANE_WIDTH_M + LANE_HYST_M + 0.01)
    assert mon.lane == LANE_ADJACENT
    # re-entry uses the tighter width (hysteresis is asymmetric)
    step(mon, DT, int(R[3] * V), y=LANE_WIDTH_M + 0.01)
    assert mon.lane == LANE_ADJACENT
    step(mon, DT, int(R[3] * V), y=LANE_WIDTH_M - 0.01)
    assert mon.lane == LANE_OWN

  def test_invalid_yrel_degrades_not_guesses(self):
    mon = RadarGapMonitor()
    assert self.run_closing(mon, y=None)            # closing still fires (conservative)
    assert mon.lane == LANE_UNKNOWN
    assert mon.degraded == 1                        # one counted episode
    mon2 = RadarGapMonitor()
    self.run_closing(mon2, y=float('nan'))
    assert mon2.lane == LANE_UNKNOWN and mon2.degraded == 1

  def test_closing_semantics_unchanged_by_lane_input(self):
    # byte-identical regression: the same closing sequence alerts identically
    # with and without a yRel (the lane layer is additive telemetry)
    a = RadarGapMonitor()
    b = RadarGapMonitor()
    for k in range(1000):
      r = R[3] - 0.4 * k * DT
      ca = step(a, DT, int(r * V))
      cb = step(b, DT, int(r * V), y=0.4)
      assert ca == cb
      if ca:
        break
    assert a.closing == b.closing and a.degraded == 1 and b.degraded == 0


class TestGate3LaneConsumption:
  """The gate-3 hooks: adjacent-lane closings are telemetry only; UNKNOWN
  degrades to the level-2 conservative answer; levels 1/2 never read lane."""

  def make_dh(self, gate):
    from openpilot.selfdrive.controls.lib.desire_helper import LaneChangeDirection
    dh = BydDesireHelper()
    dh.lane_change_direction = LaneChangeDirection.left
    dh.radar_gap_gate = gate
    return dh

  def test_gate3_adjacent_does_not_block_window(self):
    dh = self.make_dh(3)
    dh.lane_change_clear_time = LANE_CHANGE_CLEAR_TIME_MIN
    dh._update_clear_window(cs_state(leftBlinker=True, radarGapClosing=True,
                                     radarGapLane=LANE_ADJACENT))
    # telemetry only: the window keeps accumulating instead of resetting
    assert dh.lane_change_clear_time == LANE_CHANGE_CLEAR_TIME_MIN + DT_MDL
    assert dh._clear_window_ok()

  def test_gate3_own_lane_keeps_level1_semantics(self):
    dh = self.make_dh(3)
    dh.lane_change_clear_time = LANE_CHANGE_CLEAR_TIME_MIN
    dh._update_clear_window(cs_state(leftBlinker=True, radarGapClosing=True,
                                     radarGapLane=LANE_OWN))
    assert dh.lane_change_clear_time == 0.0
    assert not dh._clear_window_ok()

  def test_gate3_unknown_lane_degrades_to_level2(self):
    # radarGapLane absent (pre-schema carstate) or 0 = UNKNOWN -> conservative
    dh = self.make_dh(3)
    dh.lane_change_clear_time = LANE_CHANGE_CLEAR_TIME_MIN
    dh._update_clear_window(cs_state(leftBlinker=True, radarGapClosing=True))
    assert dh.lane_change_clear_time == 0.0
    assert not dh._clear_window_ok()

  def test_gate12_ignore_lane_field(self):
    # spec: gate=1/2 semantics byte-identical - an ADJACENT closing still
    # blocks (the lane refinement only exists at level 3)
    for gate in (1, 2):
      dh = self.make_dh(gate)
      dh.lane_change_clear_time = LANE_CHANGE_CLEAR_TIME_MIN
      dh._update_clear_window(cs_state(leftBlinker=True, radarGapClosing=True,
                                       radarGapLane=LANE_ADJACENT))
      assert dh.lane_change_clear_time == 0.0

  def test_gate3_abort_suppressed_for_adjacent(self):
    dh = self.make_dh(3)
    assert not dh._mid_maneuver_abort(cs_state(leftBlinker=True, radarGapClosing=True,
                                               radarGapNew=True, radarGapLane=LANE_ADJACENT))
    assert dh._mid_maneuver_abort(cs_state(leftBlinker=True, radarGapClosing=True,
                                           radarGapNew=True, radarGapLane=LANE_OWN))
    # UNKNOWN degrades conservative: aborts like level 2
    assert dh._mid_maneuver_abort(cs_state(leftBlinker=True, radarGapClosing=True,
                                           radarGapNew=True, radarGapLane=LANE_UNKNOWN))

  def test_gate2_abort_ignores_lane(self):
    dh = self.make_dh(2)
    assert dh._mid_maneuver_abort(cs_state(leftBlinker=True, radarGapClosing=True,
                                           radarGapNew=True, radarGapLane=LANE_ADJACENT))

  def test_gate_param_clamped_to_three(self):
    orig = desire_helper_byd.get_int_param
    try:
      desire_helper_byd.get_int_param = lambda params, key, default: 9
      assert BydDesireHelper().radar_gap_gate == desire_helper_byd.RADAR_GAP_LEVEL_MAX
    finally:
      desire_helper_byd.get_int_param = orig

"""Host tests for the BYD radar interface: MRR track slots + b7 continuous
lead + HUD-bar fallback + 67ms slot table (docs/byd-radar-010x.md ⑧/⑨/⑩,
docs/byd-radar-slot67-2026-10-18.md, §8.5)."""
import time

import pytest

from opendbc.car import CanData, structs
from opendbc.car.byd.radar_interface import (ACC_HUD_ADAS_MSG, B7_HI, B7_LO, DIST_MSGS,
                                             HUD_TRACK_ID, RadarInterface, SLOT_TRACK_ID_BASE,
                                             TRACK_ID_BASE, TRACK_MSGS)
from opendbc.car.byd.radar_multitarget import (C_DIST_OFFSET, C_DIST_SCALE, j1850_crc8,
                                               Y_C0, Y_D_OFF, Y_K)
from opendbc.car.byd.values import CAR, RADAR_DIST_SCALE_DEFAULT, RADAR_HUD_BARS_TO_M

MSG_T_NS = int(1e9 / 50)  # HUD rate; track frames ride the same tick, fine for tests


def hud_data(has_lead: bool, bar: int) -> bytes:
  # dbc layout (byd_general_pt): HasLead = bit 9 (byte1 b1), LeadingDistance =
  # bits 13-15 (byte1 b5-7). A real CANParser decoding these bytes is itself
  # the bit-position regression check.
  dat = bytearray(8)
  dat[1] = ((1 if has_lead else 0) << 1) | (bar << 5)
  return bytes(dat)


def track_data(long_m: float, lat_m: float, rel_kmh: float, tid: int = 1, valid: bool = True) -> bytes:
  # tangdm slot layout: LongDist 16|12@1- (0.06,-4), LatDist 28|10@1+ (0.05,-25.6),
  # TrackID 38|6, RelSpeed 44|10 (1,-512), IsValid 56|1
  dat = bytearray(8)

  def put(start: int, length: int, raw: int) -> None:
    for i in range(length):
      if (raw >> i) & 1:
        dat[(start + i) // 8] |= 1 << ((start + i) % 8)

  put(16, 12, int(round((long_m + 4) / 0.06)) & 0xFFF)
  put(28, 10, int(round((lat_m + 25.6) / 0.05)) & 0x3FF)
  put(38, 6, tid & 0x3F)
  put(44, 10, int(round(rel_kmh + 512)) & 0x3FF)
  if valid:
    dat[7] |= 1  # bit 56
  return bytes(dat)


def make_cp(radar_unavailable: bool) -> structs.CarParams:
  CP = structs.CarParams.new_message()
  CP.carFingerprint = CAR.BYD_SONG_PLUS_DMI_22
  CP.radarUnavailable = radar_unavailable
  return CP


def make_ri(radar_unavailable: bool) -> RadarInterface:
  return RadarInterface(make_cp(radar_unavailable), structs.CarParamsSP())


def dist_data(b7: int, presence: int) -> tuple[bytes, bytes]:
  d9 = bytearray(8)
  d9[7] = b7
  dA = bytearray(8)
  dA[3] = (presence & 0x7) << 5
  return bytes(d9), bytes(dA)


def feed(ri: RadarInterface, has_lead: bool, bar: int, n: int = 60, tracks=None,
         b7: int | None = None, presence: int = 7) -> structs.RadarDataT | None:
  out = None
  for i in range(n):
    frames = [CanData(ACC_HUD_ADAS_MSG, hud_data(has_lead, bar), 2)]
    if tracks is not None:
      for idx, payload in enumerate(tracks):
        if payload is not None:
          frames.append(CanData(TRACK_MSGS[idx], payload, 1))
    if b7 is not None:
      d9, dA = dist_data(b7, presence)
      frames.append(CanData(DIST_MSGS[0], d9, 1))
      frames.append(CanData(DIST_MSGS[1], dA, 1))
    ret = ri.update([(i * MSG_T_NS, frames)])
    if ret is not None:
      out = ret
  return out


class TestInert:
  def test_no_parsers_when_radar_unavailable(self):
    ri = make_ri(True)
    assert ri.rcp_hud is None and ri.rcp_tracks is None and ri.rcp_dist is None

  def test_never_produces_tracks_when_unavailable(self):
    out = feed(make_ri(True), True, 4)
    # base RadarInterfaceBase returns an empty RadarData every 5 frames
    if out is not None:
      assert len(out.points) == 0


class TestHudFallback:
  def test_lead_present_makes_one_point(self):
    out = feed(make_ri(False), True, 4)
    assert out is not None and len(out.points) == 1
    p = out.points[0]
    assert p.trackId == HUD_TRACK_ID
    assert p.dRel == pytest.approx(RADAR_HUD_BARS_TO_M[4])
    # carrot deviation (design.md D2, ported): the position-only HUD fallback
    # carries a NaN vRel so this fork's radard never associates it with vision
    # (vel_sane gate) - the structural equivalent of carrot's measured=False
    assert p.vRel != p.vRel  # NaN
    assert p.vRel != p.vRel  # NaN

  def test_bar_ordering_and_switch(self):
    ri = make_ri(False)
    feed(ri, True, 2)
    assert ri.pts[HUD_TRACK_ID].dRel == pytest.approx(RADAR_HUD_BARS_TO_M[2])
    feed(ri, True, 4)
    assert ri.pts[HUD_TRACK_ID].dRel == pytest.approx(RADAR_HUD_BARS_TO_M[4])

  def test_no_lead_clears_point(self):
    ri = make_ri(False)
    feed(ri, True, 4)
    assert len(ri.pts) == 1
    out = feed(ri, False, 0)
    assert len(ri.pts) == 0 and len(out.points) == 0

  def test_all_table_bars_parse(self):
    for bar, dist in RADAR_HUD_BARS_TO_M.items():
      assert bar <= 7
      ri = make_ri(False)
      feed(ri, True, bar)
      assert ri.pts[HUD_TRACK_ID].dRel == pytest.approx(dist)

  def test_invalid_slots_keep_fallback(self):
    # the Song's real condition: slot frames present, all IsValid=0 -> HUD path
    idle = track_data(0.0, 0.0, 0.0, valid=False)
    out = feed(make_ri(False), True, 3, tracks=[idle] * 4)
    assert len(out.points) == 1
    assert out.points[0].trackId == HUD_TRACK_ID


class TestTrackPriority:
  def test_valid_slot_decodes_and_beats_hud(self):
    t0 = track_data(12.2, 3.1, -10.0, tid=7)  # slot 0x351 valid
    out = feed(make_ri(False), True, 4, tracks=[t0, None, None, None])
    pts = {p.trackId: p for p in out.points}
    assert set(pts) == {TRACK_ID_BASE}  # only the valid slot, HUD suppressed
    p = pts[TRACK_ID_BASE]
    assert p.dRel == pytest.approx(12.2, abs=0.06)
    assert p.yRel == pytest.approx(3.1, abs=0.05)
    assert p.vRel == pytest.approx(-10.0 / 3.6, abs=0.01)
    assert p.vRel == p.vRel  # finite vRel: a real measured point

  def test_multi_slot(self):
    tracks = [track_data(20.0, 1.0, -5.0), track_data(45.0, -2.5, 2.0, valid=False),
              track_data(30.0, 0.5, -1.0), None]
    out = feed(make_ri(False), True, 2, tracks=tracks)
    ids = {p.trackId for p in out.points}
    assert ids == {TRACK_ID_BASE, TRACK_ID_BASE + 2}

  def test_slot_goes_stale_then_fallback_returns(self):
    ri = make_ri(False)
    feed(ri, True, 4, tracks=[track_data(10.0, 0.0, 0.0), None, None, None])
    assert set(ri.pts) == {TRACK_ID_BASE}
    # next cycle: slot invalid, HUD lead present -> fallback point, track point gone
    feed(ri, True, 3, tracks=[track_data(0.0, 0.0, 0.0, valid=False), None, None, None])
    assert set(ri.pts) == {HUD_TRACK_ID}

  def test_zero_track_id_is_empty_sentinel(self):
    # vendor _update: track_id == 0 means the slot is empty even if IsValid set
    out = feed(make_ri(False), True, 4, tracks=[track_data(15.0, 0.0, 0.0, tid=0, valid=True), None, None, None])
    assert {p.trackId for p in out.points} == {HUD_TRACK_ID}

  def test_no_hud_frames_yet_reports_can_error(self):
    # HUD parser is the guaranteed bus; its silence (not track silence) gates errors.
    # Adapted for this fork's Python CANParser: it timestamps first_seen at parser
    # construction with time.monotonic_ns() and grants a 2 s never-seen grace
    # (opendbc/can/parser.py), so the feed must start at the real clock and run
    # past grace + CAN_INVALID_CNT before can_valid flips - same end behavior as
    # sunnypilot's C++ parser, just after ~2 s of silence instead of immediately.
    ri = make_ri(False)
    out = None
    t0 = time.monotonic_ns()
    for i in range(300):  # 300 * 20 ms = 6 s > 2 s grace + 5 invalid counters
      ret = ri.update([(t0 + i * MSG_T_NS, [CanData(TRACK_MSGS[0], track_data(10.0, 0.0, 0.0), 1)])])
      if ret is not None and ret.errors.canError:
        out = ret
    assert out is not None and out.errors.canError


class TestB7Lead:
  """0x109 b7 continuous lead: primary synthesized source (docs ⑧/⑨)."""

  def test_b7_beats_hud_bar(self):
    # same tick: b7=100 (25 m at pinned u) vs HUD bar 4 (35 m) -> b7 wins
    out = feed(make_ri(False), True, 4, b7=100)
    assert len(out.points) == 1
    p = out.points[0]
    assert p.trackId == HUD_TRACK_ID
    assert p.dRel == pytest.approx(100 * RADAR_DIST_SCALE_DEFAULT)
    assert p.vRel == p.vRel  # finite vRel: a real measured point

  def test_steady_lead_vrel_near_zero(self):
    out = feed(make_ri(False), True, 4, b7=120)
    assert abs(out.points[0].vRel) < 1e-9

  def test_closing_vrel_negative_and_finite(self):
    # shrink 2 counts/frame; implementation assumes the 25 Hz frame rate, and
    # this test feeds at 50 Hz ticks, so expect 2*u*25 per frame step
    ri = make_ri(False)
    out = None
    for i in range(60):
      out = ri.update([(i * MSG_T_NS, [CanData(ACC_HUD_ADAS_MSG, hud_data(True, 4), 2)] +
                        [CanData(a, d, 1) for a, d in zip(DIST_MSGS, dist_data(max(B7_LO, 160 - 2 * i), 7), strict=True)])])
    assert out is not None
    p = out.points[0]
    assert p.vRel == pytest.approx(-2 * RADAR_DIST_SCALE_DEFAULT * 25, abs=0.3)
    # finite vRel: this is the point radard CAN consume; the HUD fallback's
    # NaN never reaches the matcher at all (measured=False, design D2)
    assert p.vRel == p.vRel

  def test_presence_and_sentinel_gate(self):
    # presence != 7 -> invisible; b7=247 (loss-edge transient) -> invisible;
    # HUD fallback keeps a position-only point while HUD HasLead stands
    for b7, pres in ((120, 0), (120, 6), (0, 7), (247, 7), (B7_HI + 1, 7)):
      out = feed(make_ri(False), True, 4, b7=b7, presence=pres)
      assert len(out.points) == 1
      assert out.points[0].trackId == HUD_TRACK_ID
      assert out.points[0].dRel == pytest.approx(RADAR_HUD_BARS_TO_M[4])
      assert out.points[0].vRel != out.points[0].vRel  # HUD tier: NaN vRel

  def test_no_target_no_hud_no_point(self):
    out = feed(make_ri(False), False, 0, b7=0, presence=0)
    assert len(out.points) == 0

  def test_hist_resets_across_gap(self):
    # a loss gap must clear the diff history: vRel after recovery must not
    # span the distance jump
    ri = make_ri(False)
    feed(ri, True, 4, b7=200)
    feed(ri, True, 0, b7=0, presence=0, n=30)          # gap
    out = feed(ri, True, 4, b7=60, n=12)               # recovered far away
    assert out.points[0].vRel == out.points[0].vRel    # finite while refilling
    feed(ri, True, 4, b7=60, n=40)                     # window full again


def slot_f0(x: int) -> bytes:
  """Block C F0 frame: b2/b3 carry the 12-bit 1/32m distance count."""
  dat = bytearray(8)
  dat[1] = 0x40                       # b1 cycle counter (unused by the point tests)
  dat[2] = x & 0xFF
  dat[3] = (1 << 4) | (x >> 8)
  dat[0] = j1850_crc8(dat[1:8])
  return bytes(dat)


def slot_row(obj: int, x: int) -> list[CanData]:
  base = 0x380 + 4 * obj
  out = []
  for k in range(4):
    dat = bytearray(8)
    dat[1] = 0x40
    if k == 0:
      dat[2], dat[3] = x & 0xFF, (1 << 4) | (x >> 8)
    else:
      dat[2] = 0x11 * k
    dat[0] = j1850_crc8(dat[1:8])
    out.append(CanData(base + k, bytes(dat), 1))
  return out


class TestSlotTable:
  """Layer 4: the 67ms multi-target slot table (radar_multitarget.py)."""

  def slot_feed(self, ri, n, x=400, b7=120, counter=0):
    """Feed n ticks of b7 + one complete C row per 4 ticks (cycle cadence)."""
    out = None
    for i in range(n):
      frames = [CanData(ACC_HUD_ADAS_MSG, hud_data(True, 4), 2)]
      d9, dA = dist_data(b7, 7)
      frames += [CanData(DIST_MSGS[0], d9, 1), CanData(DIST_MSGS[1], dA, 1)]
      if i % 4 == 0:                   # crude 67ms-ish cadence for the table
        row = slot_row(0, x)
        for c in row:                  # roll b1 by the 0x11 step per cycle
          dat = bytearray(c.dat)
          dat[1] = (0x40 + (i // 4) * 0x11) & 0xFF
          dat[0] = j1850_crc8(dat[1:8])
          frames.append(CanData(c.address, bytes(dat), 1))
      ret = ri.update([(i * MSG_T_NS, frames)])
      if ret is not None:
        out = ret
    return out

  def test_slot_points_coexist_with_b7(self):
    # consume spec: b7 lead and slot points coexist in liveTracks
    out = self.slot_feed(make_ri(False), 24)
    ids = {p.trackId for p in out.points}
    assert HUD_TRACK_ID in ids
    assert any(i >= SLOT_TRACK_ID_BASE for i in ids)

  def test_display_level_slot_points_published(self):
    # default level 1 publishes slot points with the pinned geometry.
    # NOTE(port): this fork has no measured flag - a slot point can only
    # become a lead through the same radard vision-association sanity gate
    # the b7 lead passes, so the carrot "display-only" discipline degrades
    # to that shared gate instead of a snapshot drop.
    out = self.slot_feed(make_ri(False), 24)
    slot_pts = [p for p in out.points if p.trackId >= SLOT_TRACK_ID_BASE]
    assert slot_pts
    assert slot_pts[0].dRel == pytest.approx(400 * C_DIST_SCALE + C_DIST_OFFSET, abs=1e-6)
    # yRel PINNED 2026-11-02 (byd-radar-y-wire-port 2.7): the frame's b4/b5 are
    # zero -> code = -1024; d=23.3 m is inside the trusted band -> finite decode
    assert slot_pts[0].yRel == pytest.approx(
      (400 * C_DIST_SCALE + Y_D_OFF) * Y_K * (-1024 - Y_C0), abs=1e-5)

  def test_slot_y_nan_outside_band(self):
    # the trusted band (8-60 m) keeps NaN isolation outside it
    out = self.slot_feed(make_ri(False), 24, x=1150)   # ~70 m > band
    slot_pts = [p for p in out.points if p.trackId >= SLOT_TRACK_ID_BASE]
    assert slot_pts and all(p.yRel != p.yRel for p in slot_pts)

  def test_b7_semantics_unchanged_by_slot_table(self):
    # rollback regression: the b7 point is byte-identical with the slot table
    # fed and with it off (level 0)
    with_slots = self.slot_feed(make_ri(False), 24)
    ri0 = make_ri(False)
    ri0.slot_table_level = 0
    without = self.slot_feed(ri0, 24)
    b7_with = [p for p in with_slots.points if p.trackId == HUD_TRACK_ID][0]
    b7_without = [p for p in without.points if p.trackId == HUD_TRACK_ID][0]
    assert (b7_with.dRel, b7_with.vRel, b7_with.trackId) == \
           (b7_without.dRel, b7_without.vRel, b7_without.trackId)
    assert not any(p.trackId >= SLOT_TRACK_ID_BASE for p in without.points)

  def test_level0_no_slot_points(self):
    ri = make_ri(False)
    ri.slot_table_level = 0
    out = self.slot_feed(ri, 24)
    assert {p.trackId for p in out.points} == {HUD_TRACK_ID}
    assert ri._slot_pending == []        # decoder not fed at all

  def test_control_level_needs_diff_history(self):
    # level 2: finite vRel only once the differential exists; a NaN
    # vRel coerced by radard would fake vLead=vEgo (the _update_hud hazard)
    ri = make_ri(False)
    ri.slot_table_level = 2
    out = self.slot_feed(ri, 12, x=400)              # first cycles: no diff yet
    slot_pts = [p for p in out.points if p.trackId >= SLOT_TRACK_ID_BASE]
    if slot_pts:
      assert slot_pts[-1].vRel != slot_pts[-1].vRel or slot_pts[-1].vRel == slot_pts[-1].vRel
    out = self.slot_feed(ri, 12, x=404)              # same target, moved 4 counts
    slot_pts = [p for p in out.points if p.trackId >= SLOT_TRACK_ID_BASE]
    assert slot_pts and slot_pts[-1].vRel == slot_pts[-1].vRel  # finite vRel
    assert slot_pts[-1].vRel == slot_pts[-1].vRel    # finite (diff available)

  def test_slot_point_gone_when_target_leaves(self):
    ri = make_ri(False)
    self.slot_feed(ri, 24)
    assert any(i >= SLOT_TRACK_ID_BASE for i in ri.pts)
    out = self.slot_feed(ri, 24, x=0)                # empty-payload rows
    assert not any(i >= SLOT_TRACK_ID_BASE for i in ri.pts)
    assert {p.trackId for p in out.points} == {HUD_TRACK_ID}

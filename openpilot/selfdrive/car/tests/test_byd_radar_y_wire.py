"""Host tests for the wire-direct lateral-y channel (byd-radar-y-wire-port 3.x).

Layers:
  1. decoder y decode: C-F0 bits 35..45 -> code (offset binary, zero 1024) ->
     y = (base + d_off)*K*(code - c0); trusted band 8-60 m, outside -> None.
  2. carstate D3 pairing: C-F0 rows ride the gap parser; per-row (d, y) latched
     at its own frame; nearest |d_row - b7*0.25| within GAP_Y_PAIR_GATE_M wins;
     no candidate -> y_rel=None -> monitor degrades to LANE_UNKNOWN.
  3. lane classification with the injected y_rel (OWN / ADJACENT / UNKNOWN).

The constants are the 2026-11-02 verdict constants (radar_multitarget.py Y_*):
engine-verified 427/427 records (|Δy| median 11.8 mm) + vision aggregate gate
holdout median 0.135 m / bias -0.006 m over the 8-60 m vendor envelope.
"""
import pytest

from opendbc.car import CanData
from opendbc.car.byd.carstate import CarState
from opendbc.car.byd.radar_gap import LANE_ADJACENT, LANE_OWN, LANE_UNKNOWN
from opendbc.car.byd.radar_multitarget import (
  COUNTER_STEP, C_DIST_SCALE, MultitargetDecoder,
  Y_BAND_MAX_M, Y_BAND_MIN_M, Y_C0, Y_CODE_ZERO, Y_D_OFF, Y_K, j1850_crc8)
from opendbc.car import structs
from opendbc.car.structs import CarParams


def y_of(x: int, code: int) -> float:
  """The pinned projection (design D2 closed form; dbc raw twin consumers
  compute exactly this)."""
  return ((x & 0x7FF) * C_DIST_SCALE + Y_D_OFF) * Y_K * (code - Y_C0)


def x_for_d(d: float) -> int:
  return round((d + 1.671) / C_DIST_SCALE)


class Seq:
  def __init__(self, start=0x40):
    self.b1 = start

  def take(self):
    b = self.b1
    self.b1 = (b + COUNTER_STEP) % 256
    return b


def mk_frame(addr: int, body: bytes, b1: int) -> tuple:
  dat = bytearray(8)
  dat[1] = b1
  dat[2:8] = body
  dat[0] = j1850_crc8(dat[1:8])
  return (0, [(addr, bytes(dat), 1)])


def c_f0_body(x: int, code11: int) -> bytes:
  """C-F0 payload: b2/b3 = 12-bit distance raw, b4/b5 = 11-bit lateral code."""
  return bytes([x & 0xFF, (x >> 8) & 0xF,
                ((code11 & 0x1F) << 3) & 0xFF, (code11 >> 5) & 0x3F, 0, 0])


def b7_body(b7: int) -> bytes:
  """0x109 payload: byte7 = distance counts (MSG_109_B7)."""
  return bytes([0, 0, 0, 0, 0, b7])


def presence_body(presence: int) -> bytes:
  """0x10A payload: b3[7:5] = Presence_10A (body[1] lands on dat[3])."""
  return bytes([0, presence << 5, 0, 0, 0, 0])


# ── layer 1: decoder y ────────────────────────────────────────────────────────

def feed_c_row(dec: MultitargetDecoder, seq: Seq, obj: int, x: int, code11: int) -> list:
  """One complete C row (F0..F3 sharing the cycle's b1) + the flush cycle;
  returns every observation the decoder emitted along the way."""
  obs = []
  b1 = seq.take()
  base = 0x380 + 4 * obj
  for i in range(4):
    obs += dec.update([mk_frame(base + i, c_f0_body(x, code11) if i == 0 else bytes(6), b1)])
  # flush: a frame with the NEXT b1 closes the cycle
  obs += dec.update([mk_frame(0x381, bytes(6), seq.take())])
  return obs


def c_obs(obs: list, obj: int = 0) -> list:
  return [o for o in obs if o['block'] == 'C' and o['obj'] == obj]


class TestDecoderY:
  def test_y_formula_on_band_row(self):
    dec = MultitargetDecoder()
    seq = Seq()
    x, code11 = 300, 1024 + 40   # d ~ 17.0 m, code +40
    rows = c_obs(feed_c_row(dec, seq, 0, x, code11))
    assert len(rows) == 1
    o = rows[0]
    assert o['y_code'] == 40
    assert o['y_rel'] == pytest.approx(y_of(x, 40), abs=1e-9)

  def test_y_none_below_band(self):
    dec = MultitargetDecoder()
    seq = Seq()
    x = x_for_d(5.0)             # ~5 m < Y_BAND_MIN
    rows = c_obs(feed_c_row(dec, seq, 0, x, 1024 + 40))
    assert rows and rows[0]['y_rel'] is None and rows[0]['d'] < Y_BAND_MIN_M

  def test_y_none_above_band(self):
    dec = MultitargetDecoder()
    seq = Seq()
    x = x_for_d(70.0)            # ~70 m > Y_BAND_MAX
    rows = c_obs(feed_c_row(dec, seq, 0, x, 1024 + 40))
    assert rows and rows[0]['y_rel'] is None and rows[0]['d'] > Y_BAND_MAX_M

  def test_non_c_blocks_carry_no_y(self):
    dec = MultitargetDecoder()
    seq = Seq()
    b1 = seq.take()
    # block B row (0x340, 2 frames): b3 = 100 -> 10.0 m near-field
    out = []
    out += dec.update([mk_frame(0x340, bytes([100, 0, 0, 0, 0, 0]), b1)])
    out += dec.update([mk_frame(0x341, bytes(6), b1)])
    out += dec.update([mk_frame(0x342, bytes(6), seq.take())])
    b_rows = [o for o in out if o['block'] == 'B']
    if b_rows:
      assert b_rows[0]['y_rel'] is None and b_rows[0]['y_code'] is None


# ── layer 2: carstate D3 pairing ─────────────────────────────────────────────

def make_cs() -> CarState:
  CP = CarParams.new_message()
  CP.carFingerprint = 'BYD_SONG_PLUS_DMI_22'
  return CarState(CP, structs.CarParamsSP())


class TestCarStatePairing:
  def test_hit_nearest_row_in_gate(self):
    cs = make_cs()
    seq = Seq()
    # b7 = 120 counts = 30.0 m; rows at 29.6 m (code +40) and 31.0 m (code -40)
    cs.update_radar_gap([mk_frame(0x10A, presence_body(7), seq.take())], 10.0)
    cs.update_radar_gap([mk_frame(0x109, b7_body(120), seq.take())], 10.0)
    cs.update_radar_gap([mk_frame(0x380, c_f0_body(x_for_d(29.6), 1024 + 40), seq.take())], 10.0)
    cs.update_radar_gap([mk_frame(0x384, c_f0_body(x_for_d(31.0), 1024 - 40), seq.take())], 10.0)
    before = cs.gap_y_pair[0]
    y = cs._slot_y_rel(set(), 120, 7)
    assert y is not None
    assert y == pytest.approx(y_of(x_for_d(29.6), 40), abs=1e-6)  # nearest wins
    assert cs.gap_y_pair[0] == before + 1

  def test_no_candidate_returns_none(self):
    cs = make_cs()
    seq = Seq()
    # row at 29.6 m, b7 lead at 40.0 m -> |Δd| ~ 10.4 m > gate
    cs.update_radar_gap([mk_frame(0x10A, presence_body(7), seq.take())], 10.0)
    cs.update_radar_gap([mk_frame(0x380, c_f0_body(x_for_d(29.6), 1024 + 40), seq.take())], 10.0)
    assert cs._slot_y_rel(set(), 160, 7) is None
    assert cs.gap_y_pair[0] == 0

  def test_band_excluded_row_is_not_candidate(self):
    cs = make_cs()
    seq = Seq()
    # row at ~5 m (below the band): matches the b7 distance but carries no y
    cs.update_radar_gap([mk_frame(0x10A, presence_body(7), seq.take())], 10.0)
    cs.update_radar_gap([mk_frame(0x380, c_f0_body(x_for_d(5.0), 1024 + 40), seq.take())], 10.0)
    assert cs._slot_y_rel(set(), 20, 7) is None     # b7 = 20 counts = 5.0 m

  def test_b7_invalid_no_pairing(self):
    cs = make_cs()
    seq = Seq()
    cs.update_radar_gap([mk_frame(0x10A, presence_body(7), seq.take())], 10.0)
    cs.update_radar_gap([mk_frame(0x380, c_f0_body(x_for_d(29.6), 1024 + 40), seq.take())], 10.0)
    for bad in (0, 246, 255):
      assert cs._slot_y_rel(set(), bad, 7) is None

  def test_stale_row_does_not_pair(self):
    cs = make_cs()
    seq = Seq()
    cs.update_radar_gap([mk_frame(0x10A, presence_body(7), seq.take())], 10.0)
    cs.update_radar_gap([mk_frame(0x380, c_f0_body(x_for_d(29.6), 1024 + 40), seq.take())], 10.0)
    cs._gap_step += 20                           # past the freshness window
    assert cs._slot_y_rel(set(), 120, 7) is None
    # presence=0: no lead -> the supply is off entirely
    cs2 = make_cs()
    cs2.update_radar_gap([mk_frame(0x10A, presence_body(0), seq.take())], 10.0)
    cs2.update_radar_gap([mk_frame(0x380, c_f0_body(x_for_d(29.6), 1024 + 40), seq.take())], 10.0)
    assert cs2._slot_y_rel(set(), 120, 0) is None
    assert not cs2._gap_y_rows


# ── layer 3: end-to-end lane classification with y_rel ───────────────────────

class TestLaneFromY:
  def _drive(self, x: int, code11: int, b7: int, presence=7):
    cs = make_cs()
    seq = Seq()
    cs.update_radar_gap([mk_frame(0x10A, presence_body(presence), seq.take())], 10.0)
    cs.update_radar_gap([mk_frame(0x109, b7_body(b7), seq.take())], 10.0)
    cs.update_radar_gap([mk_frame(0x380, c_f0_body(x, code11), seq.take())], 10.0)
    cs.update_radar_gap([mk_frame(0x381, bytes(6), seq.take())], 10.0)
    return cs

  def test_own_lane(self):
    # lead ~30 m with y ~ +0.6 m -> inside the own band
    cs = self._drive(x_for_d(30.0), Y_CODE_ZERO + 20, b7=120)
    assert cs.gap_mon.lane == LANE_OWN

  def test_adjacent_lane(self):
    # y = 30 m * K * (152+15) ~ +2.5 m -> beyond the own band + hysteresis
    cs = self._drive(x_for_d(30.0), Y_CODE_ZERO + 152, b7=120)
    assert cs.gap_mon.lane == LANE_ADJACENT

  def test_unknown_without_pair(self):
    # b7 far from the row -> LANE_UNKNOWN degradation
    cs = self._drive(x_for_d(30.0), Y_CODE_ZERO + 20, b7=200)
    assert cs.gap_mon.lane == LANE_UNKNOWN

  def test_unknown_without_presence(self):
    cs = self._drive(x_for_d(30.0), Y_CODE_ZERO + 20, b7=120, presence=0)
    assert cs.gap_mon.lane == LANE_UNKNOWN

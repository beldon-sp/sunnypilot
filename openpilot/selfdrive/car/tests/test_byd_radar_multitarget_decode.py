"""Host tests for the bus1 67ms multi-target slot-table decoder
(opendbc/car/byd/radar_multitarget.py; spec byd-radar-multitarget-decode).

Transport layer: J1850 CRC gate, b1 global counter cycle discipline.
Assembly: complete object rows produce points, empty slots stay silent,
range-edge counts are invalid. Identity: distance-gated pairing keeps a
target's trackId across row moves; unpairable re-births get a fresh id.

Cycle timing note: a cycle is finalized when the FIRST frame of the NEXT b1
arrives, so tests drive a Sequencer and flush with a placeholder cycle.
"""
import pytest

from opendbc.car.byd.radar_multitarget import (
  COUNTER_STEP, C_DIST_OFFSET, C_DIST_SCALE, MultitargetDecoder,
  VREL_QUANT_FLOOR_MPS, j1850_crc8)


def c_d(x: int) -> float:
  """Engine decode (PINNED 2026-10-31): d = 0.062319*x - 1.671."""
  return (x & 0x7FF) * C_DIST_SCALE + C_DIST_OFFSET

BASE_B1 = 0x40


class Sequencer:
  """Rolling b1 counter, one step per fed cycle."""

  def __init__(self, start=BASE_B1):
    self.b1 = start

  def take(self):
    b = self.b1
    self.b1 = (b + COUNTER_STEP) % 256
    return b


def frame(payloads: dict[int, bytes], b1: int) -> list:
  frames = []
  for addr, body in payloads.items():
    dat = bytearray(8)
    dat[1] = b1
    dat[2:8] = body
    dat[0] = j1850_crc8(dat[1:8])
    frames.append((0, [(addr, bytes(dat), 1)]))
  return frames


def c_f0_body(x: int, f3_high: int = 1) -> bytes:
  """Block C F0 payload: x=(b3&0xF)<<8|b2 (12 bit distance count)."""
  return bytes([x & 0xFF, (f3_high << 4) | (x >> 8), 0, 0, 0, 0])


def c_row(obj: int, x: int) -> dict[int, bytes]:
  base = 0x380 + 4 * obj
  return {base: c_f0_body(x), base + 1: b'\1\0\0\0\0\0',
          base + 2: b'\0\0\0\0\0\0', base + 3: b'\0\0\0\0\0\0'}


def feed(dec, payloads: dict[int, bytes], seq: Sequencer):
  return dec.update(frame(payloads, seq.take()))


def advance(dec, seq: Sequencer, cycles=1, x=200, obj=0):
  """Feed `cycles` clean cycles then a placeholder flush; return the last
  real cycle's output (the flush cycle itself emits nothing)."""
  out = []
  for _ in range(cycles):
    out = feed(dec, c_row(obj, x), seq)
  out = feed(dec, c_row(obj, 0), seq)   # empty-payload flush cycle
  return [o for o in out if o['valid'] or not o['valid']]


class TestTransport:
  def test_crc_corrupt_frame_dropped(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    corrupt = frame(c_row(0, 100), seq.take())
    t, frames = corrupt[0]
    dat = bytearray(frames[0][1])
    dat[2] ^= 0x01                      # flip a payload bit, keep stale CRC
    corrupt[0] = (t, [(0x380, bytes(dat), 1)])
    out = dec.update(corrupt)           # only this frame: cycle never closes
    assert out == [] and dec.stats['crc_errors'] == 1

  def test_crc_corrupt_does_not_poison_rest_of_cycle(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    mixed = frame(c_row(0, 100), seq.take())
    t, frames = mixed[1]                # corrupt the F1 frame (0x381)
    dat = bytearray(frames[0][1])
    dat[3] ^= 0xFF
    mixed[1] = (t, [(0x381, bytes(dat), 1)])
    dec.update(mixed)
    out = feed(dec, c_row(0, 101), seq)
    # the corrupt cycle closes with 3/4 frames -> incomplete; the next full
    # cycle assembles normally (no permanent poisoning). The finishing update
    # carries the NEXT cycle's payload; the x=101 row completes one update later.
    assert out == [] and dec.stats['C_incomplete'] == 1
    out2 = feed(dec, c_row(0, 102), seq)
    assert len(out2) == 1 and out2[0]['d'] == pytest.approx(c_d(101), abs=1e-9)

  def test_counter_break_discards_inflight_cycle(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    dec.update(frame(c_row(0, 100), seq.take()))
    broken_b1 = (seq.b1 + COUNTER_STEP) % 256   # skip one cycle
    out = dec.update(frame(c_row(1, 50), broken_b1))
    seq.b1 = (broken_b1 + COUNTER_STEP) % 256
    assert dec.stats['counter_breaks'] == 1
    assert out == []                    # voided cycle (x=200) emits nothing
    out = feed(dec, c_row(1, 51), seq)
    # the broken-b1 cycle (x=50) was a legitimate new cycle: it completes here
    assert out and out[0]['d'] == pytest.approx(c_d(50), abs=1e-9)


class TestAssembly:
  def test_complete_row_produces_point(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    out = advance(dec, seq, cycles=2, x=225)
    assert len(out) == 1
    o = out[0]
    assert o['d'] == pytest.approx(c_d(225), abs=1e-9)
    assert o['valid'] and o['track_id'] >= 201

  def test_empty_slot_stays_silent(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    out = []
    for _ in range(3):
      out = feed(dec, c_row(0, 0), seq)
    assert out == [] and dec.stats['C_empty'] >= 1

  def test_incomplete_row_no_point(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    payloads = c_row(0, 100)
    payloads.pop(0x383)                 # F3 missing
    feed(dec, payloads, seq)
    out = feed(dec, c_row(0, 101), seq)
    assert out == [] and dec.stats['C_incomplete'] == 1

  def test_edge_band_dropped(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    out = advance(dec, seq, cycles=2, x=0xFFF)   # bit27 set -> edge
    # edge-band observations are discarded (spec: measured=False or discard)
    assert out == [] and dec.stats['C_edge'] >= 1

  def test_block_b_near_field(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    out = []
    for _ in range(2):
      out = feed(dec, {0x340: bytes([0, 50, 0, 0, 0, 0]),   # b3=50 -> 5.0 m
                       0x341: b'\0' * 6}, seq)
    assert out and out[0]['d'] == pytest.approx(5.0)

  def test_block_d_assembled_but_isolated(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    out = []
    for _ in range(2):
      payloads = {0x3C8 + k: bytes([0x11 * (k + 1)] + [0] * 5) for k in range(4)}
      out = feed(dec, payloads, seq)
    # block D fields are HYPOTHESIS-isolated: no points, assembly counted
    assert out == [] and dec.stats['D_assembled'] == 1


class TestIdentity:
  def test_track_id_stable_across_cycles(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    out1 = advance(dec, seq, cycles=3, x=200)
    out2 = advance(dec, seq, cycles=1, x=200)
    assert out1 and out2
    assert out1[0]['track_id'] == out2[0]['track_id']

  def test_row_move_keeps_track_id(self):
    # same target drifting from object row 0 to row 1, distance-continuous
    dec, seq = MultitargetDecoder(), Sequencer()
    feed(dec, c_row(0, 200), seq)
    feed(dec, c_row(0, 202), seq)
    old_id = dec.tracks[0].track_id
    feed(dec, c_row(1, 204), seq)          # finishes the row-0 x=202 cycle
    out = feed(dec, c_row(1, 206), seq)    # finishes the row-1 x=204 cycle
    assert out and out[0]['obj'] == 1
    assert out[0]['track_id'] == old_id    # paired across the row move

  def test_unpairable_rebirth_new_track_id(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    feed(dec, c_row(0, 200), seq)
    out = advance(dec, seq, cycles=2, x=60)
    assert out and out[0]['track_id'] != dec.tracks[0].track_id
    assert dec.stats['track_new'] >= 2    # no id reuse after death/rebirth

  def test_vrel_diff_and_quantization_floor(self):
    dec, seq = MultitargetDecoder(), Sequencer()
    feed(dec, c_row(0, 200), seq)
    feed(dec, c_row(0, 200), seq)
    feed(dec, c_row(0, 201), seq)          # +1 count per cycle, contiguous
    out = feed(dec, c_row(0, 0), seq)      # flush finishes the x=201 cycle
    assert out and out[0]['v_rel'] is not None
    assert out[0]['v_rel'] == pytest.approx(C_DIST_SCALE / 0.06716, abs=1e-3)
    assert abs(out[0]['v_rel']) >= VREL_QUANT_FLOOR_MPS - 1e-6

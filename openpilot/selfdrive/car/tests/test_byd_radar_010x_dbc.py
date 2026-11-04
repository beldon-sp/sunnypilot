"""CANParser bit-order regression for the bus1 0x10x radar block in byd_general_pt.dbc.

The 0x101-0x10A frames (docs/byd-radar-010x.md) are registered in the DBC but
deliberately NOT parsed on the car (radar_interface.py untouched, CP.
radarUnavailable unchanged), so this file - a real CANParser fed real-shape
sample payloads - is the only guard that the dbc bit positions, the two
HYPOTHESIS signals (LongDist_109 = 0x109 b7, Presence_10A = 0x10A b3[7:5]),
the common b0/b1 layer, and the MSG_<frame>_B<n> raw placeholders stay
byte-exact. If a dbc edit moves a start bit, one of these fails.
"""
import pytest

from opendbc.can import CANParser
from opendbc.car import Bus, CanData
from opendbc.car.byd.values import CAR, DBC

BUS1 = 1
RETX_MSGS = (0x101, 0x105)      # pure transport retransmissions (99.7-100% identical)
HEALTH_MSGS = (0x102, 0x106)    # sensor-health A/B pair, all-MSG
BLOCK_MSGS = RETX_MSGS + HEALTH_MSGS + (0x109, 0x10A)
MSG_T_NS = int(1e9 / 25)        # block runs ~25 Hz on bus 1
SCALE = 0.25                    # u PINNED 2026-10-19 (owner decision): 0.25 m/count, see CM_ BO_ 265


def feed(payloads: dict[int, bytes], addrs: tuple[int, ...] = BLOCK_MSGS, n: int = 30) -> CANParser:
  cp = CANParser(DBC[CAR.BYD_SONG_PLUS_DMI_22][Bus.radar], [(a, 25) for a in addrs], BUS1)
  frames = [CanData(a, payloads.get(a, bytes(8)), BUS1) for a in addrs]
  for i in range(n):
    cp.update([(i * MSG_T_NS, frames)])
  return cp


def ramp(seed: int = 0) -> bytes:
  return bytes((seed + 10 + 17 * i) & 0xFF for i in range(8))


class TestLongDist109:
  def test_is_byte7_scaled_by_pinned_u(self):
    # real mined values: 41 held steady 62 s in d3da5795a9--142, 247 = loss-edge
    # transient, 0 = no target; per-bar global class means sit in the same range
    for raw in (0, 41, 139, 247):
      dat = bytearray(ramp())
      dat[7] = raw
      vl = feed({0x109: bytes(dat)}).vl[0x109]
      assert vl["LongDist_109"] == pytest.approx(raw * SCALE, abs=1e-9)

  def test_independent_of_bytes_below_b7(self):
    base = bytearray(ramp())
    for seed in range(4):
      dat = bytearray(base)
      for i in range(7):  # b0..b6 all move
        dat[i] = (dat[i] + 91 * seed) & 0xFF
      vl = feed({0x109: bytes(dat)}).vl[0x109]
      assert vl["LongDist_109"] == pytest.approx(base[7] * SCALE, abs=1e-9)

  def test_other_slots_decode_raw(self):
    dat = ramp(3)
    vl = feed({0x109: bytes(dat)}).vl[0x109]
    assert vl["MSG_10x_CheckSum"] == dat[0]          # private CRC, algorithm UNKNOWN
    assert vl["MSG_10x_AliveCounter"] == dat[1]      # rolling counter
    for i in range(2, 7):
      assert vl[f"MSG_109_B{i}"] == dat[i]           # placeholders stay factor-1 raw

  def test_raw_b7_twin_is_scale_free(self):
    # MSG_109_B7 shares b7 with LongDist_109 at factor 1: the counts path
    # (carstate.update_radar_gap -> RadarGapMonitor) must never see the
    # configurable scale (BydRadarDistScale), whatever it is set to
    for raw in (0, 15, 139, 247):
      dat = bytearray(ramp())
      dat[7] = raw
      vl = feed({0x109: bytes(dat)}).vl[0x109]
      assert vl["MSG_109_B7"] == raw
      assert vl["LongDist_109"] == pytest.approx(raw * SCALE, abs=1e-9)


class TestPresence10A:
  def test_b3_top3_bits(self):
    for bits in range(8):
      dat = bytearray(ramp())
      dat[3] = (bits << 5) | 0b10100
      vl = feed({0x10A: bytes(dat)}).vl[0x10A]
      assert vl["Presence_10A"] == bits
      assert vl["MSG_10A_B3_LOW"] == 0b10100  # low 5 bits are a separate MSG slot

  def test_lead_value_111_and_leakage(self):
    dat = bytearray([0x3A, 0x5C, 0xFF, 0xE0, 0x00, 0x55, 0xAA, 0xFF])
    vl = feed({0x10A: bytes(dat)}).vl[0x10A]
    assert vl["Presence_10A"] == 7  # 111 = forward target visible (median 98.62% lead agreement)
    # neighbouring bytes must not bleed into the 3-bit field
    dat[2] ^= 0xFF
    dat[4] ^= 0xFF
    vl = feed({0x10A: bytes(dat)}).vl[0x10A]
    assert vl["Presence_10A"] == 7
    assert vl["MSG_10A_B2"] == dat[2] and vl["MSG_10A_B4"] == dat[4]


class TestCommonLayer:
  def test_b0_b1_named_on_all_six_frames(self):
    for addr in BLOCK_MSGS:
      dat = ramp(addr & 0xF)
      vl = feed({addr: bytes(dat)}, addrs=(addr,)).vl[addr]
      assert vl["MSG_10x_CheckSum"] == dat[0]
      assert vl["MSG_10x_AliveCounter"] == dat[1]


class TestRetxDuplication:
  def test_identical_payload_identical_parse(self):
    # the documented 99.7-100% byte identity of 0x101 == 0x105 (incl. b0): a
    # single payload fed to both must yield signal-for-signal equal values
    dat = ramp(7)
    cp = feed({0x101: bytes(dat), 0x105: bytes(dat)}, addrs=(0x101, 0x105))
    a, b = cp.vl[0x101], cp.vl[0x105]
    assert a["MSG_10x_CheckSum"] == b["MSG_10x_CheckSum"] == dat[0]
    assert a["MSG_10x_AliveCounter"] == b["MSG_10x_AliveCounter"] == dat[1]
    for i in range(2, 8):
      assert a[f"MSG_101_B{i}"] == dat[i]
      assert b[f"MSG_105_B{i}"] == dat[i]

  def test_b0_only_diff_isolates_checksum(self):
    d1, d2 = bytearray(ramp(2)), bytearray(ramp(2))
    d2[0] ^= 0x01  # same payload, one checksum bit flipped
    cp = feed({0x101: bytes(d1), 0x105: bytes(d2)}, addrs=(0x101, 0x105))
    assert cp.vl[0x101]["MSG_10x_CheckSum"] != cp.vl[0x105]["MSG_10x_CheckSum"]
    for i in range(2, 8):
      assert cp.vl[0x101][f"MSG_101_B{i}"] == cp.vl[0x105][f"MSG_105_B{i}"]


class TestHealthPair:
  def test_all_payload_bytes_are_raw_placeholders(self):
    for addr in HEALTH_MSGS:
      dat = ramp(addr)
      vl = feed({addr: bytes(dat)}, addrs=(addr,)).vl[addr]
      for i in range(2, 8):
        assert vl[f"MSG_{addr:03X}_B{i}"] == dat[i]


class TestDbcLoad:
  def test_all_byd_platforms_instantiate_block(self):
    # dbc load regression for every byd fingerprint / both buses: the file must
    # parse, the six frames must exist, and the existing radar consumers (HUD +
    # tang slots) must keep their addresses (no collision with 257-266)
    for car in CAR:
      for bus in (Bus.pt, Bus.radar):
        cp = CANParser(DBC[car][bus], [(a, 25) for a in BLOCK_MSGS] +
                       [(849, 25), (850, 25), (851, 25), (852, 25), (813, 50)], BUS1)
        for addr, name in ((0x101, "RADAR_RETX_101"), (0x102, "RADAR_HEALTH_102"),
                           (0x105, "RADAR_RETX_105"), (0x106, "RADAR_HEALTH_106"),
                           (0x109, "RADAR_DIST_109"), (0x10A, "RADAR_PRESENCE_10A")):
          assert addr in cp.dbc.addr_to_msg
          assert cp.dbc.name_to_msg[name].address == addr
          assert cp.dbc.addr_to_msg[addr].name == name
        # name-keyed vl access works like address-keyed
        assert cp.vl["RADAR_DIST_109"] is cp.vl[0x109]
        assert cp.vl["RADAR_PRESENCE_10A"] is cp.vl[0x10A]

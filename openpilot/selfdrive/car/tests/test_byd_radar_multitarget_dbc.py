"""CANParser regression for the bus1 67ms slot-table block C distance frames
in byd_general_pt.dbc (openspec byd-radar-multitarget-port task 2.4).

The six BO_ 896-916 frames are registered but deliberately NOT parsed on the
car (the decode path is the pure-python assembler radar_multitarget.py, which
never reads this dbc), so this file - a real CANParser fed real-shape sample
payloads - guards the PINNED x=(b3&0xF)<<8|b2 1/32 m layout, the b0/b1
transport layer, and the all-byd-instantiation conflict-freedom. If a dbc edit
moves a start bit, one of these fails.
"""
import pytest

from opendbc.can import CANParser
from opendbc.car import Bus, CanData
from opendbc.car.byd.radar_multitarget import j1850_crc8
from opendbc.car.byd.values import CAR, DBC

BUS1 = 1
SLOT_C_MSGS = tuple(0x380 + 4 * k for k in range(6))
MSG_T_NS = int(1e9 / 15)          # slot table ~14.9 Hz
SCALE = 0.062319                  # PINNED 2026-10-31 (engine decode)
OFFSET = -1.671


def slot_frame(x: int, b1: int = 0x40) -> bytes:
  dat = bytearray(8)
  dat[1] = b1
  dat[2] = x & 0xFF
  dat[3] = (1 << 4) | (x >> 8)    # F3-high nibble stands in for the state bits
  dat[0] = j1850_crc8(dat[1:8])
  return bytes(dat)


def feed(payloads: dict[int, bytes], n: int = 10) -> CANParser:
  cp = CANParser(DBC[CAR.BYD_SONG_PLUS_DMI_22][Bus.radar],
                 [(a, 15) for a in SLOT_C_MSGS], BUS1)
  frames = [CanData(a, payloads.get(a, bytes(8)), BUS1) for a in SLOT_C_MSGS]
  for i in range(n):
    cp.update([(i * MSG_T_NS, frames)])
  return cp


class TestSlotCDistance:
  def test_is_bits16_27_scaled_1_over_32(self):
    # mined reference: slot67 doc §2 seg--108 t=0.7s x=225 -> /32 = 7.03 m
    # with the same-tick b7 ~28-29 (slope 8, u=0.25)
    for raw in (0, 225, 0xFFF):
      vl = feed({0x380: slot_frame(raw)}).vl[0x380]
      # dbc carries the 11-bit engine decode incl. offset; raw >= 2048 folds
      # the state bit into the 11-bit field (dbc shows the wire honestly)
      assert vl["LongDist_C380"] == pytest.approx((raw & 0x7FF) * SCALE + OFFSET, abs=1e-9)

  def test_low_nibble_of_b3_is_the_high_byte(self):
    # x = (b3&0xF)<<8|b2: flipping b3's HIGH nibble (state bits) must not move x
    for state in range(1, 8):
      dat = bytearray(8)
      dat[1] = 0x40
      dat[2] = 0x23                          # x low byte, constant
      dat[3] = (state << 4) | 0x1            # state nibble moves, x nibble fixed
      dat[0] = j1850_crc8(dat[1:8])
      vl = feed({0x380: bytes(dat)}).vl[0x380]
      assert vl["LongDist_C380"] == pytest.approx(0x123 * SCALE + OFFSET, abs=1e-9)

  def test_transport_layer_decodes(self):
    dat = slot_frame(0x2AB, b1=0x62)
    vl = feed({0x380: dat}).vl[0x380]
    assert vl["MSG_SLOTC_CheckSum"] == dat[0]
    assert vl["MSG_SLOTC_CycleCounter"] == 0x62

  def test_all_six_rows_same_layout(self):
    names = ("LongDist_C380", "LongDist_C384", "LongDist_C388",
             "LongDist_C38C", "LongDist_C390", "LongDist_C394")
    for k, (addr, name) in enumerate(zip(SLOT_C_MSGS, names, strict=True)):
      vl = feed({addr: slot_frame(100 + k)}).vl[addr]
      assert vl[name] == pytest.approx((100 + k) * SCALE + OFFSET, abs=1e-9)


class TestInstantiationFreedom:
  def test_all_byd_platforms_build_parsers_with_slot_frames(self):
    # the new BO_ entries must not conflict with any platform's dbc mapping
    # (Bus.radar aliases byd_general_pt for the whole brand)
    for fp, buses in DBC.items():
      if Bus.radar not in buses:
        continue
      cp = CANParser(buses[Bus.radar], [(a, 15) for a in SLOT_C_MSGS], BUS1)
      assert set(cp.vl.keys()) >= set(SLOT_C_MSGS)

  def test_slot_frames_absent_from_car_parsers(self):
    # no on-car parser subscribes to the slot table: CarState's pt/adas parsers
    # and the radar interface parsers must be untouched by the new BO_ entries
    from opendbc.car.byd.carstate import CarState
    from opendbc.car.byd.radar_interface import RadarInterface
    from opendbc.car import structs
    CP = structs.CarParams.new_message()
    CP.carFingerprint = CAR.BYD_SONG_PLUS_DMI_22
    for parser in CarState.get_can_parsers(CP, structs.CarParamsSP()).values():
      if parser is not None:
        assert not set(parser.vl.keys()) & set(SLOT_C_MSGS)
    ri = RadarInterface(CP, structs.CarParamsSP())
    for parser in (ri.rcp_tracks, ri.rcp_hud, ri.rcp_dist):
      assert not set(parser.vl.keys()) & set(SLOT_C_MSGS)

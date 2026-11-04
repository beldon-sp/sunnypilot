from collections import defaultdict

import pytest

from opendbc.can import CANPacker, CANParser
from opendbc.car import Bus, CanData, structs
from opendbc.car.byd.carstate import AVAIL_FALL_DEBOUNCE_TIME, BOOT_LATCH_HOLD_TIME, CarState
from opendbc.car.byd.values import CAR, DBC


class FakeParser:
  """CANParser stand-in: all signals read 0 unless set. CarState only touches
  .vl[message][signal]."""

  def __init__(self):
    self.vl = defaultdict(lambda: defaultdict(float))


def make_carstate() -> CarState:
  CP = structs.CarParams.new_message()
  CP.carFingerprint = CAR.BYD_SONG_PLUS_DMI_22
  return CarState(CP, structs.CarParamsSP())


def run_update(cs: CarState, main: bool = True, session: bool = True, brake: bool = False):
  """main = AccOn1 (stalk main switch posture); session = AccControlActive
  (radar commanding). Independent on the wire - brake cancel and CANCEL drop
  the session while main stays up."""
  pt, adas = FakeParser(), FakeParser()
  if main:
    adas.vl['ACC_HUD_ADAS']['AccOn1'] = 1
    adas.vl['ACC_HUD_ADAS']['AccState'] = 1
  if session:
    adas.vl['ACC_CMD']['AccControlActive'] = 1
    adas.vl['ACC_HUD_ADAS']['AccState'] = 2
    adas.vl['ACC_HUD_ADAS']['AccOn1'] = 1
  if brake:
    pt.vl['DRIVE_STATE']['BrakePressed'] = 1
  ret, ret_sp = cs.update({Bus.pt: pt, Bus.adas: adas})
  return ret


HOLD_FRAMES = int(BOOT_LATCH_HOLD_TIME * 100)
DEBOUNCE_FRAMES = int(AVAIL_FALL_DEBOUNCE_TIME * 100)


class TestBydBootLatchHold:
  """Boot-mid-cruise: the stock ACC is engaged when the device boots.

  cruiseState.enabled must stay False through the boot hold and then produce
  ONE genuine rising edge once the hold expires (vendor mechanism, route
  7--12e) - reporting the raw latch immediately eats the edge in the generic
  layer (canValid / ready window NO_ENTRY) and OP never engages.
  """

  def test_hold_delays_the_engage_edge(self):
    cs = make_carstate()
    for _ in range(10):
      ret = run_update(cs, main=True, session=True)
      assert not ret.cruiseState.enabled        # latch held low
      assert ret.cruiseState.available          # available stays live

    cs.boot_frames = HOLD_FRAMES - 2            # last held frame (+1 in update)
    ret = run_update(cs, main=True, session=True)
    assert not ret.cruiseState.enabled
    ret = run_update(cs, main=True, session=True)   # hold expires -> the one edge
    assert ret.cruiseState.enabled
    ret = run_update(cs, main=True, session=True)   # stays engaged, no flapping
    assert ret.cruiseState.enabled

  def test_set_during_hold_is_absorbed_into_hold_end_edge(self):
    cs = make_carstate()
    for _ in range(10):
      run_update(cs, main=True, session=False)  # main before SET, no session yet
    cs.boot_frames = HOLD_FRAMES - 3
    ret = run_update(cs, main=True, session=True)   # SET lands during the hold
    assert not ret.cruiseState.enabled
    ret = run_update(cs, main=True, session=True)   # still held
    assert not ret.cruiseState.enabled
    ret = run_update(cs, main=True, session=True)   # hold end -> the one edge
    assert ret.cruiseState.enabled

  def test_no_session_no_edge(self):
    # ignition leftover: main posture without a session this drive must never arm
    cs = make_carstate()
    cs.boot_frames = HOLD_FRAMES + 100
    for _ in range(5):
      ret = run_update(cs, main=True, session=False)
      assert not ret.cruiseState.enabled
      assert ret.cruiseState.available


class TestBydMainLatch:
  """cruiseState.enabled = debounced main-on + one genuine session this drive.
  Brake and session drops deliberately do not clear it; only main-off does."""

  def test_brake_does_not_exit(self):
    cs = make_carstate()
    for _ in range(HOLD_FRAMES + 2):
      run_update(cs, main=True, session=True)
    assert cs.is_cruise_latch
    ret = run_update(cs, main=True, session=True, brake=True)   # session drops on brake
    assert ret.cruiseState.enabled

  def test_session_standby_does_not_exit(self):
    # CANCEL / bounce: radar leaves the engaged states, main stays up
    cs = make_carstate()
    for _ in range(HOLD_FRAMES + 2):
      run_update(cs, main=True, session=True)
    assert cs.is_cruise_latch
    for _ in range(200):
      ret = run_update(cs, main=True, session=False)
      assert ret.cruiseState.enabled
      assert ret.cruiseState.available

  def test_main_off_exits(self):
    cs = make_carstate()
    for _ in range(HOLD_FRAMES + 2):
      run_update(cs, main=True, session=True)
    assert cs.is_cruise_latch
    for _ in range(DEBOUNCE_FRAMES + 2):
      ret = run_update(cs, main=False, session=False)
    assert not ret.cruiseState.enabled
    assert not ret.cruiseState.available

  def test_bounce_blip_does_not_flap_available(self):
    # the camera's ~2 s main glitches (route 11 t=156.4 / route 12 t=200.3)
    # must not flap cruiseState.available - but a real main-off (longer than
    # the debounce) must still come through
    cs = make_carstate()
    for _ in range(HOLD_FRAMES + 2):
      run_update(cs, main=True, session=True)
    for _ in range(DEBOUNCE_FRAMES - 1):
      ret = run_update(cs, main=False, session=False)
      assert ret.cruiseState.available          # held through the drop
      assert ret.cruiseState.enabled
    ret = run_update(cs, main=True, session=True)   # recovers: no edge was lost
    assert ret.cruiseState.enabled

    for _ in range(DEBOUNCE_FRAMES + 2):            # sustained main-off
      ret = run_update(cs, main=False, session=False)
    assert not ret.cruiseState.available
    assert not ret.cruiseState.enabled


class TestBydStalkBlinkers:
  """TURN_SIGNAL_SWITCH side mapping - the vendor decodes it as two SETS
  (carstate.py.disasm 10706/10756): (2, 3) = left, (4, 5) = right, 1 = neutral.

  Road test 2026-10-05..07 (sunnypilot, 415-segment raw sweep) on the 宋: a
  flick reads transient-then-detent - right = 4 then 5, left = 2 then 3 - so
  BOTH members of a side must light that side. The earlier single-value guess
  (5 = left, 4 = right) inverted the sides: lane changes fired only off the
  0.2-0.3 s right transient and a real left hold (3) decoded as no blinker at
  all ("right works, left does nothing"). Ported from sunnypilot 839d871ae8.
  """

  @pytest.mark.parametrize("switch, expected", [
    (1, (False, False)),   # neutral
    (2, (True, False)),    # left transient (stalk moving)
    (3, (True, False)),    # left held detent
    (4, (False, True)),    # right transient
    (5, (False, True)),    # right held detent
    (0, (False, False)),   # unused positions read as "no blinker"
    (6, (False, False)),
    (7, (False, False)),
  ])
  def test_side_sets(self, switch, expected):
    cs = make_carstate()
    pt, adas = FakeParser(), FakeParser()
    pt.vl['STALKS']['TURN_SIGNAL_SWITCH'] = switch
    ret, ret_sp = cs.update({Bus.pt: pt, Bus.adas: adas})
    assert (bool(ret.leftBlinker), bool(ret.rightBlinker)) == expected

  def test_swap_override(self):
    # BydBlinkerStalkSwap=1 exchanges the two sets (other BYD stalk layouts)
    cs = make_carstate()
    cs.blinker_stalk_swap = True
    for switch, expected in [(2, (False, True)), (5, (True, False))]:
      pt, adas = FakeParser(), FakeParser()
      pt.vl['STALKS']['TURN_SIGNAL_SWITCH'] = switch
      ret, ret_sp = cs.update({Bus.pt: pt, Bus.adas: adas})
      assert (bool(ret.leftBlinker), bool(ret.rightBlinker)) == expected


class TestBydYawRate:
  """bus0 0x222 YAW_RATE -> carState.yawRate (openspec byd-fingerprint-yaw-hardening;
  dbc from carrot2, three-way validated against kinematic yaw on the real car:
  corr 0.90-0.97, slope ~1.0, left positive - docs/byd-carrot2-reference-2026-10-21.md s7.6)."""

  def run_yaw(self, yaw_rate: float, yaw_offset: float) -> float:
    cs = make_carstate()
    pt, adas = FakeParser(), FakeParser()
    pt.vl['YAW_RATE']['YawRate'] = yaw_rate
    pt.vl['YAW_RATE']['YawRateOffset'] = yaw_offset
    ret, ret_sp = cs.update({Bus.pt: pt, Bus.adas: adas})
    return float(ret.yawRate)

  def test_parser_init_zero_is_zero(self):
    # vl before the first frame is the 0.0 init (parser.py _add_message
    # signals_dict) - straight difference, stays 0.0
    assert self.run_yaw(0.0, 0.0) == 0.0

  def test_all_zero_raw_guard_is_zero(self):
    # ECU-not-ready pattern: factor/offset on raw 0/0 decodes to pure
    # garbage (-2.094)-(-0.13) = -1.964 rad/s - never a real sample
    # (straight driving idles at mid-scale raw ~981/61)
    assert self.run_yaw(-2.094, -0.13) == 0.0

  def test_difference_and_left_positive_sign(self):
    # yawRate is a capnp float32 field - tolerance follows that precision
    assert self.run_yaw(-1.99, -2.09) == pytest.approx(0.10, abs=1e-6)
    assert self.run_yaw(-2.09, -1.99) == pytest.approx(-0.10, abs=1e-6)


class TestBydYawDbcRoundtrip:
  """0x222/0x223 registered in byd_general_pt.dbc: packer->parser roundtrip
  pins the signal layout (Intel @1+ 12-bit fields, byte6-low Counter)"""

  def test_roundtrip(self):
    dbc = DBC[CAR.BYD_SONG_PLUS_DMI_22][Bus.pt]
    pk = CANPacker(dbc)
    cp = CANParser(dbc, [("YAW_RATE", 50), ("AXAY", 50)], 0)
    t_ns = 0
    for i in range(10):
      frames = []
      addr, dat, src = pk.make_can_msg("YAW_RATE", 0,
                                       {"YawRate": 0.10, "YawRateOffset": -0.02, "Counter": i})
      frames.append(CanData(addr, dat, src))
      addr, dat, src = pk.make_can_msg("AXAY", 0,
                                       {"Ax": 1.0, "AxOffset": 0.0, "Counter": i})
      frames.append(CanData(addr, dat, src))
      cp.update([(t_ns, frames)])
      t_ns += 20_000_000
    # 12-bit resolution: YAW 0.002133 rad/s per count, AX 0.027 m/s2 per count
    assert cp.vl["YAW_RATE"]["YawRate"] == pytest.approx(0.10, abs=0.0022)
    assert cp.vl["YAW_RATE"]["YawRateOffset"] == pytest.approx(-0.02, abs=0.0022)
    assert cp.vl["AXAY"]["Ax"] == pytest.approx(1.0, abs=0.028)
    assert int(cp.vl["YAW_RATE"]["Counter"]) == 9

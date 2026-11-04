import pytest

from opendbc.car import Bus, structs
from opendbc.car.byd.carcontroller import ACCEL_SLEW_UP, CarController
from opendbc.car.byd.values import DBC, CAR, CarControllerParams

LongCtrlState = structs.CarControl.Actuators.LongControlState

ACC_CMD_ADDR = 0x32E
ACCEL_MIN_START = CarControllerParams.MIN_START_ACCEL
# command cadence: _update_longitudinal fires every other 100 Hz frame
DT_CMD = 0.02


class FakeCS:
  """CarState stand-in: the controller only reads .out plus the cached bus-2
  dicts (radar_acc_msg/adas_msg/aeb_msg) and the lateral EPS flags."""

  def __init__(self):
    self.out = structs.CarState()
    self.radar_acc_msg = {}
    self.adas_msg = {}
    self.aeb_msg = {}
    self.cam_lkas = {}
    self.cruise_activated = False
    self.torque_failed = False
    self.steer_error = 0
    self.steer_warning = False


def make_controller() -> CarController:
  CP = structs.CarParams.new_message()
  CP.carFingerprint = CAR.BYD_SONG_PLUS_DMI_22
  CP.openpilotLongitudinalControl = True
  dbc_names = {Bus.pt: DBC[CP.carFingerprint][Bus.pt]}
  return CarController(dbc_names, CP, structs.CarParamsSP())


def make_cc(accel: float, long_active: bool = True, enabled: bool = True, jerk: float = 0.0,
            state: LongCtrlState = LongCtrlState.pid, resume: bool = False) -> structs.CarControl:
  CC = structs.CarControl()
  CC.enabled = enabled
  CC.latActive = False
  CC.longActive = long_active
  CC.actuators.accel = accel
  CC.actuators.jerk = jerk
  CC.actuators.longControlState = state
  CC.cruiseControl.resume = resume
  return CC.as_reader()   # the controller calls actuators.as_builder(), a reader method


def step(cc, cs: FakeCS, controller: CarController) -> dict:
  """Run one 100 Hz update pair; return the decoded fields of the 50 Hz
  0x32E the controller sent (AccelCmd byte 0, JerkUpper byte 3, JerkLower /
  Resume byte 4, control bits byte 5)."""
  dat = None
  for _ in range(2):
    _, can_sends = controller.update(cc, structs.CarControlSP(), cs, 0)
    for addr, d, _bus in can_sends:
      if addr == ACC_CMD_ADDR:
        dat = d
  assert dat is not None, "no 0x32E sent"
  return {
    "accel": dat[0] * 0.05 - 5.0,
    # JerkUpper 24|7@1+ -> byte3 bits0-6; JerkLower 32|7@1+ -> byte4 bits0-6;
    # ResumeFromStandstill 39|1@0 -> byte4 bit7 (matches safety GET_BIT 44 etc.)
    "jerk_up": (dat[3] & 0x7F) * 0.2,
    "jerk_low": (dat[4] & 0x7F) * 0.2 - 16.0,
    "resume": (dat[4] >> 7) & 1,
    "standstill": dat[5] & 1,
    "brake_behavior": (dat[5] >> 1) & 3,
    "req_not_ss": (dat[5] >> 3) & 1,
    "active": (dat[5] >> 4) & 1,
  }


def radar_frame(active: bool, accel: float = 0.0, req: bool | None = None,
                standstill: bool = False) -> dict:
  if req is None:
    req = active and not standstill
  return {
    "AccelCmd": accel, "ComfortBandUpper": 0.0, "ComfortBandLower": 0.0,
    "SETME1_0x1": 1, "JerkUpperLimit": 0.0, "ResumeFromStandstill": 0,
    "JerkLowerLimit": 0.0, "StandstillState": int(standstill), "BrakeBehaviour": 0,
    "AccReqNotStandstill": int(req), "AccControlActive": int(active),
    "AccOverrideOrStandstill": 0, "EspBehaviour": 1,
  }


def adas_frame(set_speed: float = 40.0) -> dict:
  return {"SetSpeed": set_speed, "HasLead": 0, "SetDistance": 3, "LeadingDistance": 0,
          "AEB": 0, "FCW": 0, "SETME1_0x1": 1, "AccState": 2, "AccOn1": 1,
          "CloseWarning": 0, "SETME2_0x1": 1, "Notify": 0, "Status": 4, "SETME3_0xFFF": 0xFFF}


def aeb_frame() -> dict:
  return {"PAYLOAD": 0}


def make_cs(active=True, accel=0.0, set_speed=40.0, **kw) -> FakeCS:
  cs = FakeCS()
  cs.radar_acc_msg = radar_frame(active, accel, **{k: kw[k] for k in ("req", "standstill") if k in kw})
  cs.adas_msg = adas_frame(set_speed)
  cs.aeb_msg = aeb_frame()
  return cs


class TestBydLongitudinalSessionGate:
  """The vendor armature gate (op_byd decompile): AccControlActive AND
  AccReqNotStandstill. No genuine control request -> no OP longitudinal,
  ever (设置/standstill-hold must not control)."""

  def test_set_without_session_does_not_command(self):
    c = make_controller()
    cs = make_cs(active=False, accel=-2.5)
    step(make_cc(accel=1.0), cs, c)
    # echo of the radar's own -2.5, not our +1.0 request
    assert step(make_cc(accel=1.0), cs, c)["accel"] == pytest.approx(-2.5)

  def test_standstill_hold_is_not_command_authority(self):
    # AccControlActive=1 but the radar is in a standstill hold
    # (AccReqNotStandstill=0): the vendor never treats this as "control",
    # our old gate did - the standstill creep that reads as 接管
    c = make_controller()
    cs = make_cs(active=True, accel=-2.5, req=False, standstill=True)
    f = step(make_cc(accel=1.5), cs, c)
    assert f["accel"] == pytest.approx(-2.5)   # echoed, our demand ignored
    assert f["req_not_ss"] == 0                # still the radar's hold frame

  def test_brake_press_yields_to_echo(self):
    c = make_controller()
    cs = make_cs(active=True, accel=0.5)
    step(make_cc(accel=0.0), cs, c)
    cs.out.brakePressed = True
    cs.radar_acc_msg = radar_frame(active=False, accel=0.0)
    assert step(make_cc(accel=-2.0), cs, c)["accel"] == pytest.approx(0.0)

  def test_no_set_speed_never_accelerates(self):
    c = make_controller()
    cs = make_cs(active=True, accel=0.0, set_speed=0.0)   # session up, speed not yet in
    for _ in range(20):
      assert step(make_cc(accel=1.5), cs, c)["accel"] <= 0.0 + 1e-9


class TestBydLongitudinalVendorPipeline:
  """Vendor command shaping: jerk envelope from the plan, minimal-brake
  behavior bit, standstill resume bump, plus our slew continuity."""

  def test_engage_ramps_from_echoed_value(self):
    c = make_controller()
    cs = make_cs(active=False, accel=-2.0)
    step(make_cc(accel=1.0), cs, c)   # one yield frame: tracker snaps to -2.0
    assert c.accel_cmd_sent == pytest.approx(-2.0)

    cs.radar_acc_msg = radar_frame(active=True, accel=-2.0)
    # the wire carries a 0.05 m/s^2 quantum: one frame's jump is at most
    # max(slew, quantum) - a step beyond that means we broke continuity
    step_limit = max(ACCEL_SLEW_UP * DT_CMD, 0.05) + 1e-9
    prev = c.accel_cmd_sent
    for _ in range(100):
      step(make_cc(accel=1.0), cs, c)
      assert c.accel_cmd_sent - prev <= step_limit, "accel stepped beyond slew+quantum"
      prev = c.accel_cmd_sent
    assert abs(c.accel_cmd_sent - 1.0) < 1e-6   # converged to the demand

  def test_plan_jerk_drives_the_envelope(self):
    # deep deceleration demand: the vendor hands the VCU a jerk budget, not
    # the old fixed 1.0/-0.8 pair
    c = make_controller()
    cs = make_cs(active=True, accel=0.0)
    cs.out.aEgo = -0.5
    # ramp to the demand first (our slew continuity), the envelope is computed
    # from the ACTUAL on-wire accel, not the raw demand
    f = None
    for _ in range(60):
      f = step(make_cc(accel=-1.5, jerk=-2.0), cs, c)
    assert c.accel_cmd_sent == pytest.approx(-1.5)
    # jerk budget: min(0.3*can, (can-a_ego)/0.1) = -10 vs plan -2.0 -> -10
    # envelope: upper clip(-10,1,12)=1, lower clip(-10,-4,-0.8)=-4
    assert f["jerk_up"] == pytest.approx(1.0)
    assert f["jerk_low"] == pytest.approx(-4.0)

  def test_calm_demand_keeps_gentle_envelope(self):
    c = make_controller()
    cs = make_cs(active=True, accel=0.0)
    f = step(make_cc(accel=0.6, jerk=0.4), cs, c)
    # can_accel ~0.03 at the ramp start... jerk budget positive -> clipped
    # to the vendor static floor 1.0 / -0.8
    assert f["jerk_up"] == pytest.approx(1.0)
    assert f["jerk_low"] == pytest.approx(-0.8)

  def test_over_set_speed_requests_minimal_brake(self):
    c = make_controller()
    cs = make_cs(active=True, accel=0.0)
    cs.out.vEgoCluster = 20.0                 # 72 km/h cluster
    cs.out.cruiseState.speed = 11.1           # 40 km/h target
    for _ in range(40):
      f = step(make_cc(accel=1.5), cs, c)
    assert f["accel"] <= 0.0 + 1e-9
    assert f["brake_behavior"] == 1

  def test_minimal_brake_threshold_chatter_does_not_sawtooth(self):
    # 2026-10-05 second drive 1154-1187 s: cluster speed chattering across the
    # bare set-speed threshold made the (post-slew) min(accel, 0) clamp snap
    # the wire between the demand and 0.00 at ~2 Hz for 30 s straight - the
    # sustained 忽加忽刹. Now latched with a ±0.15 deadband and the clamp is
    # ahead of the slew, so the wire can only breathe at the slew rate.
    c = make_controller()
    cs = make_cs(active=True, accel=0.0)
    cs.out.cruiseState.speed = 8.3
    prev = None
    for i in range(60):
      cs.out.vEgoCluster = 8.5 if i % 4 < 2 else 8.1   # chatter over the deadband
      f = step(make_cc(accel=0.25), cs, c)
      if prev is not None:
        assert abs(f["accel"] - prev) <= 0.055, f"minimal-brake stepped {prev} -> {f['accel']} @i={i}"
      prev = f["accel"]

  def test_jerk_sign_noise_does_not_step_the_wire(self):
    # 2026-10-05 roadtest root cause of the 顿挫: plan jerk dipped to -0.5
    # while accel sat positive and DM-i aEgo overshot the command - the
    # catch-up (then living in bydcan, AFTER the slew limiter) slammed the
    # wire from +0.95 to -0.10 in ONE frame, ~22 times in 90 s of following.
    # Moved ahead of the slew and gated on demand <= 0, a positive demand
    # must never be jerked negative in one step.
    c = make_controller()
    cs = make_cs(active=True, accel=0.0)
    cs.out.aEgo = 0.0
    for _ in range(40):                      # steady positive cruise
      step(make_cc(accel=0.95, jerk=0.5), cs, c)
    prev = c.accel_cmd_sent
    assert prev == pytest.approx(0.95, abs=0.06)
    cs.out.aEgo = 1.3                        # powertrain overshoot +0.35
    for _ in range(5):
      f = step(make_cc(accel=0.95, jerk=-0.5), cs, c)
      assert f["accel"] > 0.7, f"catch-up stepped the wire: {f['accel']}"

  def test_true_undershoot_pre_brake_now_ramps(self):
    # the vendor's intent survives: a genuinely decelerating plan (demand
    # <= 0, jerk <= -0.25) under-executed by the car still tightens toward
    # 0.2*jerk - but through the slew, not as a step
    c = make_controller()
    cs = make_cs(active=True, accel=0.0)
    cs.out.aEgo = 0.0
    for _ in range(30):
      step(make_cc(accel=-0.1, jerk=-0.1), cs, c)
    prev = c.accel_cmd_sent
    cs.out.aEgo = 0.15                       # aEgo - demand = 0.25 > 0.2
    max_step = max(ACCEL_SLEW_UP * DT_CMD, 0.05) + 1e-9
    for _ in range(30):                      # floor 0.2*jerk = -0.2
      f = step(make_cc(accel=-0.1, jerk=-1.0), cs, c)
      assert abs(f["accel"] - prev) <= max_step, f"pre-brake stepped: {prev} -> {f['accel']}"
      prev = f["accel"]
    assert c.accel_cmd_sent == pytest.approx(-0.2, abs=0.06)

  def test_resume_bump_needs_the_vendor_and_chain(self):
    c = make_controller()
    # resume only pulses when the car holds a standstill the radar has
    # RELEASED (radar accel > 0, no radar standstill state) - vendor AND chain
    cs = make_cs(active=True, accel=0.5, req=True, standstill=False)
    cs.out.cruiseState.standstill = True
    for _ in range(45):   # ramp 0 -> -1.0 at the slew limit first
      step(make_cc(accel=-1.0), cs, c)
    assert c.accel_cmd_sent == pytest.approx(-1.0)
    f = step(make_cc(accel=-0.5, state=LongCtrlState.starting), cs, c)
    assert f["accel"] >= ACCEL_MIN_START - 1e-9
    assert f["resume"] == 1

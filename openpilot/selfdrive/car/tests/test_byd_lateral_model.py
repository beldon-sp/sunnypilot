"""BYD lateral: vendor siglin model port (A/B tiers of docs/byd-lateral-vendor-model.md).

Covers the torque-tune wiring (deadzone/ki) and the siglin stack against the vendor
formulas decoded from cp_byd interface.py.disasm.txt (L183/L380/L1218/L1556/L1953).
"""
from types import SimpleNamespace

import pytest

from opendbc.car import structs
from opendbc.car.byd.interface import CarInterface, non_linear_torque, sig
from opendbc.car.byd.values import CAR, CarControllerParams

# verbatim Song Plus DM-i 22 row (vendor values.py runtime dump)
F1, F2, F3, F4, F5, F6, F7 = CarControllerParams.NON_LINEAR_TORQUE_PARAMS[CAR.BYD_SONG_PLUS_DMI_22]


class StubCarInterface(CarInterface):
  """Bypass CarInterfaceBase.__init__ (no CAN parsers needed for these tests)."""

  def __init__(self, fingerprint: str = CAR.BYD_SONG_PLUS_DMI_22, cruise_activated: bool = True,
               v_ego: float = 0.0):
    self.CP = structs.CarParams(carFingerprint=fingerprint)
    # this fork's 2-arg siglin callback reads v_ego from CS.out (telemetry)
    self.CS = SimpleNamespace(cruise_activated=cruise_activated, out=SimpleNamespace(vEgo=v_ego))
    self._lat_state_is_torque = False
    self._lat_torque_i = 0.0


def torque_params(lat_accel_factor: float = 2.5) -> SimpleNamespace:
  return SimpleNamespace(latAccelFactor=lat_accel_factor, friction=2.5)


class TestBydLateralTuneWiring:
  """A档: vendor configure_torque_tune (disasm L1953) - ki=0, 0.1 deg angle deadzone."""

  def _get_params(self):
    ret = structs.CarParams()
    CarInterface._get_params(ret, CAR.BYD_SONG_PLUS_DMI_22, {}, [], False, True, False)
    return ret

  def test_torque_tuning_vendor_values(self):
    ret = self._get_params()
    assert ret.lateralTuning.which() == 'torque'
    tune = ret.lateralTuning.torque
    # NOTE(port): this fork's capnp has no torque.ki/kp/kf (the PID gains live
    # in LatControlTorque; the vendor ki=0 parity is the brand gate in
    # latcontrol_torque). The 0.1 deg deadzone still rides CP.
    assert tune.steeringAngleDeadzoneDeg == pytest.approx(0.1)

  def test_torque_params_from_override_toml(self):
    # vendor torque_data row [2.5, 2.5, 0.10] under legend
    # (LAT_ACCEL_FACTOR, MAX_LAT_ACCEL_MEASURED, FRICTION) - identical files, both repos
    ret = self._get_params()
    tune = ret.lateralTuning.torque
    assert tune.latAccelFactor == pytest.approx(2.5)
    assert tune.friction == pytest.approx(0.10)


class TestBydSig:
  def test_zero_and_bounds(self):
    assert sig(0.0) == 0.0
    assert sig(1e9) == pytest.approx(0.5)
    assert sig(-1e9) == pytest.approx(-0.5)
    assert sig(700.0) == pytest.approx(0.5)  # clamped, no OverflowError

  def test_odd_symmetry(self):
    assert sig(1.23) == pytest.approx(-sig(-1.23))


class TestBydNonLinearTorque:
  """Vendor L380: f2*sig(lat*uf2*hsf*f1*hsf*(1-uf)) + lat*uf2*v/10, left/right asymmetric."""

  def test_zero_is_zero(self):
    assert non_linear_torque(0.0, 20.0, F1, F2, F3, F4, F5, F6, F7) == 0.0

  def test_sign_follows_input(self):
    t_left = non_linear_torque(-1.0, 20.0, F1, F2, F3, F4, F5, F6, F7)
    t_right = non_linear_torque(1.0, 20.0, F1, F2, F3, F4, F5, F6, F7)
    assert t_left < 0.0 < t_right

  def test_left_stronger_than_right(self):
    # the structural road-crown pre-compensation: |left| > |right| at equal |lat accel|
    t_left = abs(non_linear_torque(-1.0, 20.0, F1, F2, F3, F4, F5, F6, F7))
    t_right = abs(non_linear_torque(1.0, 20.0, F1, F2, F3, F4, F5, F6, F7))
    assert t_left > t_right
    assert t_left == pytest.approx(0.472, abs=2e-3)   # hand-traced from the disasm
    assert t_right == pytest.approx(0.417, abs=2e-3)

  def test_monotonic_within_half_plane(self):
    prev = 0.0
    for lat in (0.2, 0.5, 1.0, 2.0, 3.0):
      t = non_linear_torque(lat, 20.0, F1, F2, F3, F4, F5, F6, F7)
      assert t > prev
      prev = t

  def test_magnitude_matches_linear_envelope(self):
    # hard-saturated lat accel (3 m/s2 at 30 m/s): siglin tops out near the vanilla
    # linear model's lat/latAccelFactor = 1.2 - exceeding 1.0 is by design, the latcontrol
    # saturation check and the STEER_MAX_REQUEST envelope are what cap the wire value
    vanilla = 3.0 / 2.5
    for lat in (-3.0, 3.0):
      t = abs(non_linear_torque(lat, 30.0, F1, F2, F3, F4, F5, F6, F7))
      assert t < 1.25
      assert t <= vanilla * 1.05


class TestBydSiglinSelector:
  def test_nonlinear_cars_pick_siglin(self):
    ci = StubCarInterface()
    assert ci.torque_from_lateral_accel().__func__ is CarInterface.torque_from_lateral_accel_siglin

  def test_other_cars_pick_linear(self):
    ci = StubCarInterface(fingerprint='BYD_HAN_SOMETHING_ELSE')
    assert ci.torque_from_lateral_accel().__func__ is not CarInterface.torque_from_lateral_accel_siglin


class TestBydAdjustedLateralAccel:
  """Vendor L1218 shaper. latAccelFactor=2.5 -> base conv 1.0; +0.3 while steer_mode false."""

  def test_steer_mode_scaling(self):
    ci = StubCarInterface()
    assert ci.get_adjusted_lateral_accel(1.0, torque_params(), True, 0.0, False) == pytest.approx(1.0)
    assert ci.get_adjusted_lateral_accel(1.0, torque_params(), False, 0.0, False) == pytest.approx(1.3)
    assert ci.get_adjusted_lateral_accel(-1.0, torque_params(), False, 0.0, False) == pytest.approx(-1.3)

  def test_gravity_adjusted_adds_roll(self):
    ci = StubCarInterface()
    out = ci.get_adjusted_lateral_accel(1.0, torque_params(), True, 0.2, True)
    assert out == pytest.approx(1.2)

  def test_curve_taper_from_integrator_log(self):
    ci = StubCarInterface()
    ci._lat_state_is_torque = True
    ci._lat_torque_i = 0.1
    # |adjusted| >= 0.2 -> full weight -> positive side damped, negative side boosted
    assert ci.get_adjusted_lateral_accel(0.5, torque_params(), True, 0.0, False) == pytest.approx(0.45)
    assert ci.get_adjusted_lateral_accel(-0.5, torque_params(), True, 0.0, False) == pytest.approx(-0.55)


class TestBydSiglinEndToEnd:
  def _call(self, lat: float, v_ego: float = 20.0, gravity: bool = False, v_floor_check: bool = False):
    # NOTE(port): this fork's callback contract is 2-arg (lateral_acceleration,
    # torque_params); v_ego comes from CS.out and friction is added by
    # latcontrol_torque outside the callback
    ci = StubCarInterface(v_ego=v_ego)
    out = ci.torque_from_lateral_accel_siglin(lat, torque_params())
    expected = non_linear_torque(lat, max(8.0, v_ego), F1, F2, F3, F4, F5, F6, F7)
    assert out == pytest.approx(expected, abs=1e-9)  # zero friction error -> pure model
    return out

  def test_matches_reference_formula(self):
    self._call(1.0)
    self._call(-1.0)
    self._call(0.5, v_ego=30.0)

  def test_speed_floor_below_29_kph(self):
    assert self._call(1.0, v_ego=8.0) == self._call(1.0, v_ego=3.0)
    assert self._call(1.0, v_ego=20.0) > self._call(1.0, v_ego=8.0)

  def test_selector_returns_callable_with_base_signature(self):
    ci = StubCarInterface()
    cb = ci.torque_from_lateral_accel()
    # same arity the latcontrol calls it with (this fork: 2-arg)
    out = cb(1.0, torque_params())
    assert isinstance(out, float)


class TestBydApplyNeverRaises:
  """Regression: the first ship of the siglin port read c.actuators.lateralControlState,
  which does not exist in the carrot CarControl schema. apply() raised every frame, card
  died, the 0x316 gap latched EPS LKAS Fault. BYD hard rule: apply() is crash-free on any
  CC (bare or fully populated), and the curve path degrades to inert instead."""

  @pytest.fixture
  def real_ci(self):
    ret = structs.CarParams.new_message()
    ret.carFingerprint = str(CAR.BYD_SONG_PLUS_DMI_22)
    CarInterface._get_params(ret, CAR.BYD_SONG_PLUS_DMI_22, {}, [], False, True, False)
    return CarInterface(ret, structs.CarParamsSP())

  def test_apply_bare_cc_does_not_raise(self, real_ci):
    # the production CC reaches apply() as a reader (carcontroller requires it)
    cc = structs.CarControl.new_message().as_reader()
    real_ci.apply(cc, structs.CarControlSP())  # must not throw
    assert real_ci._lat_state_is_torque is False  # no msgq under test shim -> inert

  def test_siglin_survives_inert_cache(self, real_ci):
    cb = real_ci.torque_from_lateral_accel()
    tune = real_ci.CP.lateralTuning.torque
    out = cb(0.5, tune)
    assert out == pytest.approx(0.5, rel=0.0, abs=1.0)  # sane finite torque, no crash

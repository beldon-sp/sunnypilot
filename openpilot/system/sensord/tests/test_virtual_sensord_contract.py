#!/usr/bin/env python3
"""Static contract tests for virtual_sensord.

These do NOT import virtual_sensord (that pulls in msgq). They lock the
safety-critical contract by inspecting the source text and by re-deriving the
pure math, so they run on a PC without scons/Linux. The runtime behaviour
still requires device-side tests (P3 gates in
docs/virtual-sensord-porting-plan-2026-11-28.md).

本分支适配：can_yaw_ok 按 CP.brand=='byd' 判定（无 extFlags 机制）、无
Hyundai 100.0 哨兵常量（BYD 垃圾帧在 carstate 端归零，sanity 门兜底）。
"""
import os
import unittest

import numpy as np

from openpilot.system.sensord.sensor_synth import (
  build_gyro, build_accel, can_yaw_ok, s2d_inv,
)

SRC = os.path.join(os.path.dirname(__file__), '..', 'virtual_sensord.py')


def source():
  with open(SRC, encoding='utf-8') as f:
    return f.read()


class TestVirtualSensordContract(unittest.TestCase):
  def test_timestamp_uses_logmono_not_wallclock(self):
    s = source()
    self.assertIn('payload.timestamp = msg.logMonoTime', s)
    # Wall-clock time would be off by uptime and fail _validate_sensor_time.
    # Check executable lines only (the docstring mentions time_ns as a warning).
    code_lines = [ln for ln in s.splitlines() if not ln.strip().startswith('#')]
    timestamp_assigns = [ln for ln in code_lines if '.timestamp =' in ln]
    self.assertTrue(timestamp_assigns)
    self.assertTrue(all('logMonoTime' in ln for ln in timestamp_assigns))

  def test_source_is_velodyne_not_impact_whitelist(self):
    s = source()
    self.assertIn('SensorSource.velodyne', s)
    self.assertNotIn('SensorSource.lsm6ds3', s)
    self.assertNotIn('SensorSource.lsm6ds3trc', s)

  def test_both_union_names_present(self):
    s = source()
    self.assertIn("'acceleration'", s)
    self.assertIn("'gyroUncalibrated'", s)  # not 'gyro'
    self.assertIn("payload.init(union_name)", s)

  def test_no_imu_calibration_exit_check(self):
    # 本分支无 ImuCalibrationEnabled 机制：可执行代码不得引用（get_bool 会抛
    # UnknownKeyName 直接崩溃）。用 ast 摘除模块 docstring 后再查。
    import ast
    tree = ast.parse(source())
    doc = ast.get_docstring(tree) or ''
    self.assertNotIn('ImuCalibrationEnabled', source().replace(doc, '', 1))

  def test_unregistered_param_falls_back_off(self):
    # manager gate must catch UnknownKeyName before libparams_c rebuild
    pcfg = os.path.join(os.path.dirname(__file__), '..', '..', 'manager', 'process_config.py')
    with open(pcfg, encoding='utf-8') as f:
      g = f.read()
    self.assertIn('def _virtual_sensord_wanted', g)
    self.assertIn('except Exception:', g)

  def test_mutually_exclusive_gates(self):
    pcfg = os.path.join(os.path.dirname(__file__), '..', '..', 'manager', 'process_config.py')
    with open(pcfg, encoding='utf-8') as f:
      g = f.read()
    self.assertIn('def virtual_sensord(', g)
    self.assertIn('def real_sensord(', g)
    self.assertIn('not imu_present()', g)
    self.assertIn('imu_present() or not _virtual_sensord_wanted', g)


class FakeCS:
  def __init__(self, yaw_rate):
    self.yawRate = yaw_rate


class FakeCP:
  def __init__(self, brand):
    self.brand = brand


class TestVirtualSensordMath(unittest.TestCase):
  def test_s2d_is_self_inverse(self):
    v = np.array([1.0, 2.0, 3.0])
    np.testing.assert_array_equal(s2d_inv(s2d_inv(v)), v)

  def test_aego_lands_on_device_x(self):
    # Call the real build_accel, then apply the same s2d that locationd does.
    a_ego, a_lat = 3.0, 0.4
    sensor = build_accel(10.0, a_lat / 10.0, a_ego, np.eye(3), True)
    device = s2d_inv(sensor)
    self.assertAlmostEqual(device[0], a_ego, places=6)      # ExcessiveActuation reads device.x
    self.assertAlmostEqual(device[1], -a_lat, places=6)
    self.assertAlmostEqual(device[2], -9.81, places=6)      # gravity reference preserved

  def test_gyro_z_sign_matches_stage2(self):
    # Left turn +0.3 rad/s must land on device z as -0.3 (vehicle yaw-up
    # is -z in the calib frame; chassis_yaw.py uses [0,0,-yaw]).
    # Camodo roll/pitch must be PRESERVED, not zeroed, when CAN yaw is used.
    camodo = [0.1, -0.05, 9.9]  # z is a sentinel: must be replaced by CAN yaw
    sensor = build_gyro(0.3, camodo, np.eye(3), True)
    device = s2d_inv(sensor)
    self.assertAlmostEqual(device[2], -0.3, places=6)
    self.assertAlmostEqual(device[0], 0.1, places=6)    # roll preserved
    self.assertAlmostEqual(device[1], -0.05, places=6)  # pitch preserved

  def test_gyro_rotated_by_mount_angle(self):
    # With a non-unit device_from_calib the yaw must be rotated, not raw.
    rpy = np.array([0.0, 0.02682, 0.00483])
    from openpilot.common.transformations.orientation import rot_from_euler
    D = rot_from_euler(rpy)
    sensor = build_gyro(0.3, [0.0, 0.0, 0.0], D, True)
    device = s2d_inv(sensor)
    self.assertAlmostEqual(np.linalg.norm(device), 0.3, places=5)  # rotation preserves norm
    self.assertAlmostEqual(device[2], -0.29989, places=4)          # matches stage-2 value

  def test_gyro_falls_back_to_camodo_when_no_can(self):
    camodo = [0.1, 0.2, 0.3]
    sensor = build_gyro(9.9, camodo, np.eye(3), False)  # CAN invalid -> camodo
    device = s2d_inv(sensor)
    np.testing.assert_allclose(device, camodo, atol=1e-6)

  def test_byd_brand_gate_and_sanity(self):
    self.assertTrue(can_yaw_ok(FakeCS(0.3), FakeCP('byd')))
    self.assertTrue(can_yaw_ok(FakeCS(0.0), FakeCP('byd')))  # 直行 0 合法
    self.assertFalse(can_yaw_ok(FakeCS(50.0), FakeCP('byd')))  # > sanity 门
    # 外部哨兵值（如 Hyundai 100.0）必须被 sanity 门拒绝
    self.assertFalse(can_yaw_ok(FakeCS(100.0), FakeCP('byd')))
    # 无解码 yaw 的品牌必须拒绝，绝不 getattr(...,0) 兜底
    self.assertFalse(can_yaw_ok(FakeCS(0.0), FakeCP('toyota')))
    self.assertFalse(can_yaw_ok(FakeCS(0.3), FakeCP('honda')))

  def test_no_getattr_zero_fallback(self):
    s = source()
    self.assertNotIn('getattr(cs', s)


if __name__ == '__main__':
  unittest.main()

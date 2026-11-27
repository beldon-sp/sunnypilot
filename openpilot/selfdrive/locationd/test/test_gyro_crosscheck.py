import unittest

from openpilot.selfdrive.locationd.gyro_crosscheck import (
  gyro_yawrate_crosscheck_valid,
  YAWRATE_CROSS_ERR_CHECK_FACTOR,
  YAWRATE_CROSSCHECK_MIN_SPEED,
)


class TestGyroYawrateCrosscheck(unittest.TestCase):

  def test_low_speed_skips_crosscheck(self):
    # parked, steering with dragged tires: gyro honestly reports yaw rate while
    # cameraOdometry reports ~0 with tiny std -> must NOT be flagged invalid
    self.assertTrue(gyro_yawrate_crosscheck_valid(0.025, 0.0, 0.0, 1e-3, 0.0))
    self.assertTrue(gyro_yawrate_crosscheck_valid(-0.05, 0.001, 0.0, 1e-4, YAWRATE_CROSSCHECK_MIN_SPEED - 1e-6))
    # same large mismatch at low speed stays valid regardless of magnitude
    self.assertTrue(gyro_yawrate_crosscheck_valid(0.2, 0.0, 0.0, 1e-3, 0.5))

  def test_driving_valid_when_gyro_matches_camodo(self):
    mean, std = 0.1, 0.01
    self.assertTrue(gyro_yawrate_crosscheck_valid(mean, 0.0, mean, std, 10.0))
    self.assertTrue(gyro_yawrate_crosscheck_valid(mean - 0.001, 0.0, mean, std, 20.0))

  def test_driving_invalid_beyond_threshold(self):
    mean, std = 0.0, 1e-3
    err = YAWRATE_CROSS_ERR_CHECK_FACTOR * std
    # beyond err threshold -> invalid
    self.assertFalse(gyro_yawrate_crosscheck_valid(err + 1e-4, 0.0, mean, std, 10.0))
    # just within threshold -> valid
    self.assertTrue(gyro_yawrate_crosscheck_valid(err - 1e-4, 0.0, mean, std, 10.0))

  def test_gyro_bias_is_accounted_for(self):
    mean, std = 0.0, 1e-3
    # gyro reading equals its bias -> zero err -> valid
    self.assertTrue(gyro_yawrate_crosscheck_valid(0.002, 0.002, mean, std, 10.0))
    # reading offset from bias beyond threshold -> invalid
    self.assertFalse(gyro_yawrate_crosscheck_valid(0.05, 0.0, mean, std, 10.0))

  def test_speed_boundary_is_exclusive(self):
    # exactly at MIN_SPEED the cross-check applies (car_speed < MIN_SPEED skips)
    mean, std = 0.0, 1e-3
    err = YAWRATE_CROSS_ERR_CHECK_FACTOR * std
    self.assertFalse(gyro_yawrate_crosscheck_valid(err + 1e-4, 0.0, mean, std, YAWRATE_CROSSCHECK_MIN_SPEED))


if __name__ == "__main__":
  unittest.main()

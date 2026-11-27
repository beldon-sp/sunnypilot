"""Gyro yaw-rate cross-check against camera-odometry yaw rate.

Parked / low-speed scenario: the vehicle can be rotated while stationary
(e.g. steering at a parking lot with dragged tires), so the IMU honestly
reports |gyro z| of a few hundredths of rad/s while cameraOdometry reports
yaw rate ~0 with a tiny std. Running the cross-check in that regime flags
every gyro frame as INPUT_INVALID, drains the sensor budget and ends in a
spurious locationdTemporaryError (SOFT_DISABLE) while parked.

Below YAWRATE_CROSSCHECK_MIN_SPEED we therefore skip the cross-check
entirely. Bad gyro sources are still blocked by the ROTATION_SANITY_CHECK
amplitude gate, which stays in effect at all speeds (see locationd.py).
"""

# factor on camodo yaw-rate std used as the cross-check error threshold
YAWRATE_CROSS_ERR_CHECK_FACTOR = 30
# below this speed (m/s) the gyro yaw-rate cross-check is skipped
YAWRATE_CROSSCHECK_MIN_SPEED = 1.0


def gyro_yawrate_crosscheck_valid(gyro_yawrate: float, gyro_bias_z: float,
                                  camodo_yawrate_mean: float, camodo_yawrate_std: float,
                                  car_speed: float) -> bool:
  """Returns True if the device-frame gyro yaw rate is consistent with cameraOdometry.

  Pure function so it can be unit-tested on PC without capnp; also shared by
  synthetic gyro sources (virtual sensord) so they get the same low-speed
  exemption for free.
  """
  # Stationary / low speed: vehicle can be rotated while parked (steering with dragged
  # tires); the gyro honestly reports that yaw rate but cameraOdometry reports ~0 with a
  # tiny std, which would drain the sensor input budget. Skip the check, rely on the
  # ROTATION_SANITY_CHECK amplitude gate.
  if car_speed < YAWRATE_CROSSCHECK_MIN_SPEED:
    return True

  err = abs((gyro_yawrate - gyro_bias_z) - camodo_yawrate_mean)
  threshold = YAWRATE_CROSS_ERR_CHECK_FACTOR * camodo_yawrate_std
  return err < threshold

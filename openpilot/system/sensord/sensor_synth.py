#!/usr/bin/env python3
"""Pure synthesis math for virtual_sensord, kept capnp-free so it is
unit-testable on a PC without msgq/Linux (like locationd/chassis_yaw.py).

All inputs/outputs follow the device/calib frame conventions of this fork's
locationd. Nothing here has been car-verified on a real route (see P3 gates in
docs/virtual-sensord-porting-plan-2026-11-28.md).
"""
import numpy as np

from openpilot.common.transformations.orientation import sensor_to_device_frame

GRAVITY = 9.81
ACCEL_LIMIT = 100.0           # below locationd ACCEL_SANITY_CHECK=100


def s2d_inv(v):
  """sensor_to_device_frame is self-inverse; call the canonical implementation."""
  return sensor_to_device_frame(v)


def build_gyro(yaw_rate: float, camodo_rot, device_from_calib: np.ndarray, chassis_yaw_ok: bool) -> np.ndarray:
  """Return gyroUncalibrated.v (sensor frame).

  Frame chain (matches chassis_yaw.py and locationd conventions):
    vehicle yaw-up  -> calib frame [0, 0, -yaw]   (device z points down)
    -> device frame via device_from_calib
    -> sensor frame via s2d inverse (s2d is self-inverse)

  locationd applies only s2d() to gyro (locationd.py:132); cameraOdometry is
  rotated by device_from_calib first (:169). So we must pre-rotate by D and
  invert s2d to land the independent CAN yaw on the correct device axis.
  Roll/pitch fall back to camodo (rot is already in the calib frame).
  """
  gyro_calib = np.asarray(camodo_rot, dtype=float).copy()
  if chassis_yaw_ok:
    # Only override the yaw axis; keep camodo roll/pitch (zeroing them
    # would feed a tight-variance false-zero to PHONE_GYRO and distort attitude).
    gyro_calib[2] = -yaw_rate
  return s2d_inv(np.asarray(device_from_calib, dtype=float) @ gyro_calib)


def build_accel(v_ego: float, yaw_rate: float, a_ego: float,
                device_from_calib: np.ndarray, chassis_yaw_ok: bool) -> np.ndarray:
  """Return acceleration.v (sensor frame).

  Gravity on sensor x maps to device z (absolute roll/pitch reference).
  a_ego goes on sensor z NEGATED so s2d puts +a_ego on device x, which is what
  ExcessiveActuationCheck reads (selfdrived/helpers.py).
  a_lat = v_ego*yaw_rate on sensor y (device y = -a_lat).

  TODO(device-verify): accel is built directly in device convention (no D
  multiply) and is exact only for near-zero mount angles. The target car
  (~0.02 rad) is within legacy calibrationd validity, but a left/right route
  must confirm a_lat sign before onroad (source has not passed this gate
  either, see its TODO(device-verify)).
  """
  a_lat = v_ego * yaw_rate if chassis_yaw_ok else 0.0
  sensor = np.array([GRAVITY, a_lat, -a_ego], dtype=float)
  return np.clip(sensor, -ACCEL_LIMIT, ACCEL_LIMIT)


def can_yaw_ok(cs, CP, sanity_check: float = 10.0) -> bool:
  """Only trust carState.yawRate where the brand proves it is decoded.

  本分支无 extFlags 机制：BYD 0x222 YAW_RATE 由 carstate 无条件解析（实车
  验证 corr 0.90-0.97），直接按 brand 判定。BYD 无 Hyundai 的 100.0 哨兵
  语义——垃圾帧已在 carstate 端归零（非就绪=0.0），此处 sanity 门兜底。

  No getattr(...,0) fallback: a brand without a decoded yaw reports
  yawRate=0 forever, which would silently zero a_lat while tests stay green
  (fork-port-audit hard rule).
  """
  if CP.brand == 'byd':
    yaw = abs(float(cs.yawRate))
    return yaw < sanity_check
  return False

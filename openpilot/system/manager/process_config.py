import os
import operator
import platform
from functools import lru_cache

from opendbc.car.structs import car
from openpilot.cereal import custom
from openpilot.common.params import Params
from openpilot.common.hardware import PC, COMMA_HARDWARE
from openpilot.system.manager.process import PythonProcess, NativeProcess, DaemonProcess
from openpilot.common.hardware.hw import Paths

from openpilot.sunnypilot.mapd.mapd_manager import MAPD_PATH

from openpilot.sunnypilot.models.helpers import get_active_model_runner
from openpilot.sunnypilot.sunnylink.utils import sunnylink_need_register, sunnylink_ready, use_sunnylink_uploader

WEBCAM = os.getenv("USE_WEBCAM") is not None
# set in launch_env.sh for the C3L clone (no cabin camera)
DISABLE_DRIVER = os.getenv("DISABLE_DRIVER") is not None

def driverview(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started or params.get_bool("IsDriverViewEnabled")

def notcar(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and CP.notCar

def iscar(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not CP.notCar

def logging(started: bool, params: Params, CP: car.CarParams) -> bool:
  run = (not CP.notCar) or not params.get_bool("DisableLogging")
  return started and run

def ublox_available() -> bool:
  return os.path.exists('/dev/ttyHS0') and not os.path.exists('/persist/comma/use-quectel-gps')

def ublox(started: bool, params: Params, CP: car.CarParams) -> bool:
  use_ublox = ublox_available()
  if use_ublox != params.get_bool("UbloxAvailable"):
    params.put_bool("UbloxAvailable", use_ublox, block=True)
  return started and use_ublox

def joystick(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("JoystickDebugMode")

# --- chassis yaw capability（BYD 0x222 YAW_RATE，carstate 无条件解析且实车验证 corr 0.90-0.97）---
def chassis_yaw(started: bool, params: Params, CP: car.CarParams) -> bool:
  # 无源库 extFlags 机制：BYD 直接按 brand 判定。镜像进 Params 供 locationd
  # （只订 carState，不订 CarParams）启用 chassis yaw KF 观测。每拍执行，
  # 恒返回 True，不影响宿主进程。
  available = CP.brand == 'byd'
  if available != params.get_bool("ChassisYawAvailable"):
    params.put_bool("ChassisYawAvailable", available, block=True)
  return True

# --- virtual sensord（无物理 IMU 设备的合成 IMU 进程，默认关）---
LSM6DS3_I2C_BUS = '/dev/i2c-1'
LSM6DS3_I2C_ADDR = 0x6A
LSM6DS3_WHO_AM_I_REG = 0x0F
LSM6DS3_WHO_AM_I = 0x6A  # LSM6DS3 / LSM6DS3TR-C 同值
I2C_SLAVE = 0x0703

@lru_cache(maxsize=1)
def imu_present() -> bool:
  # 不能用源库的 sysfs 探测（/sys/bus/i2c/devices/1-006a）：2026-11-28 设备实测
  # C3L 克隆 bus1 无 sysfs 节点而 IMU 硬件在线（sensord 走 i2c-dev 直读，
  # 不依赖内核枚举），sysfs 探测必然误判"无 IMU"——若 VirtualSensordEnabled=1
  # 会把健康的真实 sensord 换成合成数据，误判方向在危险的一侧。
  # 改用 WHO_AM_I 直读（设备上已验证）：探测失败 → 视为无 IMU；异常 →
  # 保守视为有 IMU（真实 sensord 继续跑，维持现状）。
  if PC:
    return True
  try:
    import fcntl
    fd = os.open(LSM6DS3_I2C_BUS, os.O_RDWR)
    try:
      fcntl.ioctl(fd, I2C_SLAVE, LSM6DS3_I2C_ADDR)
      os.write(fd, bytes([LSM6DS3_WHO_AM_I_REG]))
      return os.read(fd, 1)[0] == LSM6DS3_WHO_AM_I
    finally:
      os.close(fd)
  except Exception:
    return True

def _virtual_sensord_wanted(params: Params) -> bool:
  # libparams_c 未重建时 key 未注册，get_bool 抛 UnknownKeyName；
  # fallback 关，保证 manager 不因门控求值崩溃。
  try:
    return params.get_bool("VirtualSensordEnabled")
  except Exception:
    return False

def virtual_sensord(started: bool, params: Params, CP: car.CarParams) -> bool:
  return only_onroad(started, params, CP) and not imu_present() and _virtual_sensord_wanted(params)

def real_sensord(started: bool, params: Params, CP: car.CarParams) -> bool:
  # 与 virtual_sensord() 严格互补：任一时刻恰好一对 acc/gyro publisher。
  return only_onroad(started, params, CP) and (imu_present() or not _virtual_sensord_wanted(params))

def not_joystick(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not params.get_bool("JoystickDebugMode")

def long_maneuver(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("LongitudinalManeuverMode")

def lat_maneuver(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("LateralManeuverMode")

def not_long_maneuver(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not params.get_bool("LongitudinalManeuverMode")

def qcomgps(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not ublox_available()

def always_run(started: bool, params: Params, CP: car.CarParams) -> bool:
  return True

def only_onroad(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started

def navigation(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("NavigationEnabled")

def only_offroad(started: bool, params: Params, CP: car.CarParams) -> bool:
  return not started

def livestream(started: bool, params: Params, CP: car.CarParams) -> bool:
  return params.get_bool("IsLiveStreaming")

def use_copyparty(started, params, CP: car.CarParams) -> bool:
  return bool(params.get_bool("EnableCopyparty"))

def sunnylink_ready_shim(started, params, CP: car.CarParams) -> bool:
  """Shim for sunnylink_ready to match the process manager signature."""
  return sunnylink_ready(params)

def sunnylink_need_register_shim(started, params, CP: car.CarParams) -> bool:
  """Shim for sunnylink_need_register to match the process manager signature."""
  return sunnylink_need_register(params)

def use_sunnylink_uploader_shim(started, params, CP: car.CarParams) -> bool:
  """Shim for use_sunnylink_uploader to match the process manager signature."""
  return use_sunnylink_uploader(params)

def is_tinygrad_model(started, params, CP: car.CarParams) -> bool:
  """Check if the active model runner is tinygrad."""
  return bool(get_active_model_runner(params, not started) == custom.ModelManagerSP.Runner.tinygrad)

def is_stock_model(started, params, CP: car.CarParams) -> bool:
  """Check if the active model runner is stock."""
  return bool(get_active_model_runner(params, not started) == custom.ModelManagerSP.Runner.stock)

def mapd_ready(started: bool, params: Params, CP: car.CarParams) -> bool:
  return bool(os.path.exists(Paths.mapd_root()))

# mapd (a Go binary) honors HTTPS_PROXY/HTTP_PROXY for its tile downloads. Read OsmProxyUrl from
# the params directory at process start (not import time) so a param change applies on the next
# mapd restart without restarting the manager.
MAPD_PROXY_ENV = (
  'if [ -f /data/params/d/OsmProxyUrl ]; then '
  + 'P=$(cat /data/params/d/OsmProxyUrl); '
  + '[ -n "$P" ] && export HTTPS_PROXY="$P" HTTP_PROXY="$P"; '
  + 'fi; '
)

def uploader_ready(started: bool, params: Params, CP: car.CarParams) -> bool:
  if not params.get_bool("OnroadUploads"):
    return only_offroad(started, params, CP)

  return always_run(started, params, CP)

def or_(*fns):
  return lambda *args: operator.or_(*(fn(*args) for fn in fns))

def and_(*fns):
  return lambda *args: operator.and_(*(fn(*args) for fn in fns))

procs = [
  # C3L clone: no comma athena — server-driven segment pulls burn cellular data
  # DaemonProcess("manage_athenad", "openpilot.system.athena.manage_athenad", "AthenadPid"),

  NativeProcess("loggerd", "openpilot/system/loggerd", ["./loggerd"], logging),
  NativeProcess("encoderd", "openpilot/system/loggerd", ["./encoderd"], only_onroad),
  NativeProcess("stream_encoderd", "openpilot/system/loggerd", ["./encoderd", "--stream"], or_(livestream, notcar)),
  PythonProcess("logmessaged", "openpilot.system.logmessaged", always_run),

  NativeProcess("camerad", "openpilot/system/camerad", ["./camerad"], or_(driverview, livestream), enabled=not WEBCAM),
  PythonProcess("webcamerad", "openpilot.system.camerad.webcam.camerad", driverview, enabled=WEBCAM),
  PythonProcess("proclogd", "openpilot.system.proclogd", only_onroad, enabled=platform.system() != "Darwin"),
  PythonProcess("journald", "openpilot.system.journald", only_onroad, platform.system() != "Darwin"),
  PythonProcess("micd", "openpilot.system.micd", iscar),
  PythonProcess("timed", "openpilot.system.timed", always_run, enabled=not PC),

  PythonProcess("modeld", "openpilot.selfdrive.modeld.modeld", and_(only_onroad, is_stock_model)),
  PythonProcess("navd", "openpilot.selfdrive.navigation.navd", navigation),
  PythonProcess("mapsd", "openpilot.selfdrive.navigation.mapsd", navigation),
  PythonProcess("dmonitoringmodeld", "openpilot.selfdrive.modeld.dmonitoringmodeld", driverview, enabled=(WEBCAM or not PC) and not DISABLE_DRIVER),

  PythonProcess("sensord", "openpilot.system.sensord.sensord", real_sensord, enabled=not PC),
  PythonProcess("virtualsensord", "openpilot.system.sensord.virtual_sensord", virtual_sensord, enabled=not PC),
  PythonProcess("ui", "openpilot.selfdrive.ui.ui", always_run),
  PythonProcess("soundd", "openpilot.selfdrive.ui.soundd", driverview),
  PythonProcess("locationd", "openpilot.selfdrive.locationd.locationd", only_onroad),
  NativeProcess("_pandad", "openpilot/selfdrive/pandad", ["./pandad"], always_run, enabled=False),
  PythonProcess("calibrationd", "openpilot.selfdrive.locationd.calibrationd", only_onroad),
  PythonProcess("torqued", "openpilot.selfdrive.locationd.torqued", only_onroad),
  PythonProcess("controlsd", "openpilot.selfdrive.controls.controlsd", and_(not_joystick, iscar)),
  PythonProcess("joystickd", "openpilot.tools.joystick.joystickd", or_(joystick, notcar)),
  PythonProcess("selfdrived", "openpilot.selfdrive.selfdrived.selfdrived", only_onroad),
  PythonProcess("card", "openpilot.selfdrive.car.card", only_onroad),
  PythonProcess("deleter", "openpilot.system.loggerd.deleter", always_run),
  PythonProcess("dmonitoringd", "openpilot.selfdrive.monitoring.dmonitoringd", driverview, enabled=(WEBCAM or not PC) and not DISABLE_DRIVER),
  PythonProcess("qcomgpsd", "openpilot.system.qcomgpsd.qcomgpsd", qcomgps, enabled=COMMA_HARDWARE),
  PythonProcess("pandad", "openpilot.selfdrive.pandad.pandad", always_run),
  PythonProcess("paramsd", "openpilot.selfdrive.locationd.paramsd", only_onroad),
  PythonProcess("lagd", "openpilot.selfdrive.locationd.lagd", only_onroad),
  PythonProcess("ubloxd", "openpilot.system.ubloxd.ubloxd", ublox, enabled=COMMA_HARDWARE),
  PythonProcess("pigeond", "openpilot.system.ubloxd.pigeond", ublox, enabled=COMMA_HARDWARE),
  PythonProcess("plannerd", "openpilot.selfdrive.controls.plannerd", not_long_maneuver),
  PythonProcess("maneuversd", "openpilot.tools.longitudinal_maneuvers.maneuversd", long_maneuver),
  PythonProcess("lateral_maneuversd", "openpilot.tools.lateral_maneuvers.lateral_maneuversd", lat_maneuver),
  PythonProcess("radard", "openpilot.selfdrive.controls.radard", only_onroad),
  PythonProcess("hardwared", "openpilot.system.hardware.hardwared", always_run),
  PythonProcess("modem", "openpilot.common.hardware.comma.modem", always_run, enabled=COMMA_HARDWARE),
  PythonProcess("tombstoned", "openpilot.system.tombstoned", always_run, enabled=not PC),
  PythonProcess("updated", "openpilot.system.updated.updated", only_offroad, enabled=not PC),
  # C3L clone: no comma uploads (qlog/qcamera/boot/crash all stay on-device)
  # PythonProcess("uploader", "openpilot.system.loggerd.uploader", uploader_ready),
  PythonProcess("statsd", "openpilot.sunnypilot.system.statsd", always_run),

  # debug procs
  NativeProcess("bridge", "openpilot/cereal/messaging", ["./bridge"], notcar),
  PythonProcess("webrtcd", "openpilot.system.webrtc.webrtcd", or_(livestream, notcar)),
  PythonProcess("joystick", "openpilot.tools.joystick.joystick_control", and_(joystick, iscar)),

  # sunnylink <3
  DaemonProcess("manage_sunnylinkd", "openpilot.sunnypilot.sunnylink.athena.manage_sunnylinkd", "SunnylinkdPid"),
  PythonProcess("sunnylink_registration_manager", "openpilot.sunnypilot.sunnylink.registration_manager", sunnylink_need_register_shim),
  PythonProcess("statsd_sp", "openpilot.sunnypilot.sunnylink.statsd", and_(always_run, sunnylink_ready_shim)),
]

# sunnypilot
procs += [
  # Models
  PythonProcess("models_manager", "openpilot.sunnypilot.models.manager", only_offroad),
  NativeProcess("modeld_tinygrad", "openpilot/sunnypilot/modeld_v2", ["./modeld"], and_(only_onroad, is_tinygrad_model)),
  PythonProcess("modeld_warmup", "openpilot.sunnypilot.modeld_v2.warmup", only_offroad),

  # Backup
  PythonProcess("backup_manager", "openpilot.sunnypilot.sunnylink.backups.manager", and_(only_offroad, sunnylink_ready_shim)),

  # mapd
  NativeProcess("mapd", Paths.mapd_root(), ["bash", "-c", MAPD_PROXY_ENV + f"{MAPD_PATH} > /dev/null 2>&1"], mapd_ready),
  PythonProcess("mapd_manager", "openpilot.sunnypilot.mapd.mapd_manager", chassis_yaw),

  # locationd
  NativeProcess("locationd_llk", "openpilot/sunnypilot/selfdrive/locationd", ["./locationd"], only_onroad),

  # carrot (carrotpilot port)
  # carrot_navi permanently owns TCP 7714 and publishes navigation data over cereal.
  PythonProcess("carrot_navi", "openpilot.selfdrive.carrot.carrot_navi", always_run, restart_if_crash=True),

  # LAN event push service (WebSocket/SSE :8083), see openpilot/mate/server.py
  PythonProcess("mate", "openpilot.mate.server", always_run, restart_if_crash=True),

  # carrot_man: 20Hz 聚合中枢（carrotNavi/carState → carrotMan min() 决策 + 7705 发现广播）
  PythonProcess("carrot_man", "openpilot.selfdrive.carrot.carrot_man", always_run, restart_if_crash=True),
]

if os.path.exists("../../sunnypilot/sunnylink/uploader.py"):
  procs += [PythonProcess("sunnylink_uploader", "openpilot.sunnypilot.sunnylink.uploader", use_sunnylink_uploader_shim)]

if os.path.exists("../../third_party/copyparty/copyparty-sfx.py"):
  sunnypilot_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
  copyparty_args = [f"-v{Paths.crash_log_root()}:/swaglogs:r"]
  copyparty_args += [f"-v{Paths.log_root()}:/routes:r"]
  copyparty_args += [f"-v{Paths.model_root()}:/models:rw"]
  copyparty_args += [f"-v{sunnypilot_root}:/sunnypilot:rw"]
  copyparty_args += ["-p8080"]
  copyparty_args += ["-z"]
  copyparty_args += ["-q"]
  procs += [NativeProcess("copyparty-sfx", "openpilot/third_party/copyparty", ["./copyparty-sfx.py", *copyparty_args], and_(only_offroad, use_copyparty))]

managed_processes = {p.name: p for p in procs}

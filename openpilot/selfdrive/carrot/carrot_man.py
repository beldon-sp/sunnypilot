import errno
import json
import math
import os
import socket
import threading
import time
import traceback
from typing import Any, Dict, Optional

import numpy as np
import psutil
import ipaddress

import openpilot.cereal.messaging as messaging
from openpilot.cereal import log
from openpilot.common.realtime import Ratekeeper, set_core_affinity
from openpilot.common.params import Params
from openpilot.common.raw_params import get_int_param
from openpilot.common.hardware import PC


from openpilot.selfdrive.carrot.carrot_serv import CarrotServ
from openpilot.selfdrive.carrot.curve_speed import VisionCurveSpeed, curve_speed, TARGET_LAT_ACCEL, NO_LIMIT_KPH
from openpilot.selfdrive.carrot.carrot_navi_control import CarrotNaviControl, parse_carrot_navi_control
from openpilot.common.gps import get_gps_location_service

try:
  from shapely.geometry import LineString
  SHAPELY_AVAILABLE = True
except ImportError:
  SHAPELY_AVAILABLE = False

NAVI_ROUTE_MAX_POINTS = 4096
BROADCAST_INTERVAL = 1.0
BROADCAST_REMOTE_INTERVAL = 0.2
BROADCAST_NETWORK_ERROR_RETRY_INTERVAL = 5.0
BROADCAST_NETWORK_ERROR_LOG_INTERVAL = 30.0
NAVI_HTTP_PORT = 7713  # legacy HTTP7713 server removed in db5bf3146; broadcast field kept for old carrot APK


def limit_route_points(points, max_points=NAVI_ROUTE_MAX_POINTS):
    if max_points <= 0:
        return []
    count = len(points)
    if count <= max_points:
        return list(points)

    limited = []
    last_index = count - 1
    previous_index = -1
    for i in range(max_points):
        source_index = round(i * last_index / max(1, max_points - 1))
        if source_index == previous_index:
            continue
        limited.append(points[source_index])
        previous_index = source_index
    return limited

# Haversine formula to calculate distance between two GPS coordinates
#haversine_cache = {}
def haversine(lon1, lat1, lon2, lat2):
    #key = (lon1, lat1, lon2, lat2)
    #if key in haversine_cache:
    #    return haversine_cache[key]

    R = 6371000  # Radius of Earth in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    distance = 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    #haversine_cache[key] = distance
    return distance


# Get the closest point on a segment between two coordinates
def closest_point_on_segment(p1, p2, current_position):
    x1, y1 = p1
    x2, y2 = p2
    px, py = current_position

    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return p1  # p1 and p2 are the same point

    # Parameter t is the projection factor onto the line segment
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0, min(1, t))  # Clamp t to the segment

    closest_x = x1 + t * dx
    closest_y = y1 + t * dy

    return (closest_x, closest_y)


# Get path after a certain distance from the current position
def get_path_after_distance(start_index, coordinates, current_position, distance_m):
    total_distance = 0
    path_after_distance = []
    closest_index = -1
    closest_point = None
    min_distance = float('inf')

    start_index = max(0, start_index - 2)

    # 가까운 점만 탐색하도록 수정
    for i in range(start_index, len(coordinates) - 1):
        p1 = coordinates[i]
        p2 = coordinates[i + 1]
        candidate_point = closest_point_on_segment(p1, p2, current_position)
        distance = haversine(current_position[0], current_position[1], candidate_point[0], candidate_point[1])

        if distance < min_distance:
            min_distance = distance
            closest_point = candidate_point
            closest_index = i
        elif distance > min_distance and min_distance < 10:
            break

    start_index = closest_index
    # Start from the closest point and calculate the path after the specified distance
    if closest_index != -1:
        path_after_distance.append(closest_point)

        path_after_distance.append(coordinates[closest_index + 1])
        total_distance = haversine(closest_point[0], closest_point[1], coordinates[closest_index + 1][0],
                                   coordinates[closest_index + 1][1])

        # Traverse the path forward from the next point
        for i in range(closest_index + 1, len(coordinates) - 1):
            coord1 = coordinates[i]
            coord2 = coordinates[i + 1]
            segment_distance = haversine(coord1[0], coord1[1], coord2[0], coord2[1])

            if total_distance + segment_distance >= distance_m and segment_distance > 0:
                remaining_distance = distance_m - total_distance
                ratio = remaining_distance / segment_distance
                interpolated_lon = coord1[0] + ratio * (coord2[0] - coord1[0])
                interpolated_lat = coord1[1] + ratio * (coord2[1] - coord1[1])
                path_after_distance.append((interpolated_lon, interpolated_lat))
                break

            total_distance += segment_distance
            path_after_distance.append(coord2)

    return path_after_distance, start_index, closest_point


def calculate_angle(point1, point2):
    delta_lon = point2[0] - point1[0]
    delta_lat = point2[1] - point1[1]
    return math.degrees(math.atan2(delta_lat, delta_lon))

# Convert GPS coordinates to relative x, y coordinates based on a reference point and heading
def gps_to_relative_xy(gps_path, reference_point, heading_deg):
    ref_lon, ref_lat = reference_point
    relative_coordinates = []

    # Convert heading from degrees to radians
    heading_rad = math.radians(heading_deg)

    for lon, lat in gps_path:
        # Convert lat/lon differences to meters (assuming small distances for simple approximation)
        x = (lon - ref_lon) * 40008000 * math.cos(math.radians(ref_lat)) / 360
        y = (lat - ref_lat) * 40008000 / 360

        # Rotate coordinates based on the heading angle to align with the car's direction
        x_rot = x * math.cos(heading_rad) - y * math.sin(heading_rad)
        y_rot = x * math.sin(heading_rad) + y * math.cos(heading_rad)

        relative_coordinates.append((y_rot, x_rot))

    return relative_coordinates


# Calculate curvature given three points using a faster vector-based method
#curvature_cache = {}
def calculate_curvature(p1, p2, p3):
    #key = (p1, p2, p3)
    #if key in curvature_cache:
    #    return curvature_cache[key]

    v1 = (p2[0] - p1[0], p2[1] - p1[1])
    v2 = (p3[0] - p2[0], p3[1] - p2[1])

    cross_product = v1[0] * v2[1] - v1[1] * v2[0]
    len_v1 = math.sqrt(v1[0] ** 2 + v1[1] ** 2)
    len_v2 = math.sqrt(v2[0] ** 2 + v2[1] ** 2)

    if len_v1 * len_v2 == 0:
        curvature = 0
    else:
        curvature = cross_product / (len_v1 * len_v2 * len_v1)

    #curvature_cache[key] = curvature
    return curvature

class CarrotMan:
  def __init__(self):
    print("************************************************CarrotMan init************************************************")
    self.params = Params()
    self.params_memory = Params("/dev/shm/params")
    self.gps_location_service = get_gps_location_service(self.params)
    self.sm = messaging.SubMaster([
      'deviceState', 'carState', 'controlsState', 'radarState', 'longitudinalPlan', 'modelV2',
      'selfdriveState', 'carControl', self.gps_location_service,
      'navInstruction', 'carrotNavi',
    ])
    self.pm = messaging.PubMaster(['carrotMan', "navRoute", "navInstructionCarrot"])

    self.carrot_serv = CarrotServ()

    self.broadcast_ip = self.get_broadcast_address()
    self.broadcast_port = 7705
    self.carrot_man_port = 7706
    self.carrot_navi_http_port = NAVI_HTTP_PORT
    self.connection = None

    self.ip_address = "0.0.0.0"
    self.remote_addr = None

    self.vision_curve_speed = VisionCurveSpeed()
    self.carrot_curve_speed_params()

    self.is_running = True
    self.navi_points = []
    self.navi_points_start_index = 0
    self.navi_points_active = False
    self.navd_active = False
    self.carrot_navi_route_session_id = ""
    self.carrot_navi_route_sequence = -1
    self.carrot_navi_route_owned = False

    self.active_carrot_last = False

    self._rgdata_ts_lock = threading.Lock()
    self._last_rgdata_timestamp_ms = 0
    self._navi_event_lock = threading.Lock()
    self._last_navi_event: Optional[Dict[str, Any]] = None
    self._last_navi_event_by_type: Dict[str, Dict[str, Any]] = {}
    self._last_complex_crossroad: Dict[str, Any] = {}

    self.is_metric = self.params.get_bool("IsMetric")


  def get_broadcast_address(self):
    # Prefer the interface carrying the default route. Ubuntu PCs generally
    # do not have the C3-era br0 interface, while devices normally use wlan0.
    try:
      local_ip = self.get_local_ip()
      ipv4_addrs = [
        addr
        for addresses in psutil.net_if_addrs().values()
        for addr in addresses
        if addr.family == socket.AF_INET and not addr.address.startswith("127.")
      ]
      ipv4_addrs.sort(key=lambda addr: addr.address != local_ip)
      for addr in ipv4_addrs:
        if addr.broadcast:
          return addr.broadcast
        if addr.netmask:
          return str(ipaddress.ip_network(f"{addr.address}/{addr.netmask}", strict=False).broadcast_address)
    except Exception as e:
      print(f"[carrot_man] failed to resolve broadcast address: {e}")
    return "255.255.255.255"

  def get_local_ip(self):
      try:
          # 외부 서버와의 연결을 통해 로컬 IP 확인
          with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
              s.connect(("8.8.8.8", 80))  # Google DNS로 연결 시도
              return s.getsockname()[0]
      except Exception:
          return None


  # 브로드캐스트 메시지 전송
  def broadcast_version_info(self):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    frame = 0
    next_broadcast_time = 0.0
    last_network_error_log_time = 0.0

    rk = Ratekeeper(20, print_delay_threshold=None)

    while self.is_running:
      try:
        self.sm.update(0)
        carrot_navi_service_active = self.sm.alive['carrotNavi'] and self.sm.valid['carrotNavi']
        if (
          self.sm.updated['carrotNavi']
          or (self.carrot_navi_route_session_id and not carrot_navi_service_active)
        ):
          carrot_navi = parse_carrot_navi_control(self.sm['carrotNavi']) if carrot_navi_service_active else None
          self._update_carrot_navi_route(carrot_navi)
        remote_addr = self.remote_addr
        remote_ip = remote_addr[0] if remote_addr is not None else ""
        vturn_speed = self.carrot_curve_speed(self.sm)
        coords, distances, route_speed = self.carrot_navi_route()

        #print("coords=", coords)
        #print("curvatures=", curvatures)
        self.carrot_serv.update_navi(remote_ip, self.sm, self.pm, vturn_speed, coords, distances, route_speed, self.gps_location_service)

        now = time.monotonic()
        if now >= next_broadcast_time:
          next_broadcast_time = now + (BROADCAST_REMOTE_INTERVAL if remote_addr is not None else BROADCAST_INTERVAL)
          try:
            self.broadcast_ip = self.get_broadcast_address() if remote_addr is None else remote_addr[0]
            if not PC:
              ip_address = socket.gethostbyname(socket.gethostname())
            else:
              ip_address = self.get_local_ip()
            if ip_address is None:
              raise OSError(errno.ENETUNREACH, "Network is unreachable")
            if ip_address != self.ip_address:
              self.ip_address = ip_address
              self.remote_addr = None

            msg = self.make_send_message()
            if self.broadcast_ip is not None:
              dat = msg.encode('utf-8')
              sock.sendto(dat, (self.broadcast_ip, self.broadcast_port))
            #for i in range(1, 255):
            #  ip_tuple = socket.inet_aton(self.broadcast_ip)
            #  new_ip = ip_tuple[:-1] + bytes([i])
            #  address = (socket.inet_ntoa(new_ip), self.broadcast_port)
            #  sock.sendto(dat, address)

            if remote_addr is None:
              #print(f"Broadcasting: {self.broadcast_ip}") #:{msg}")
              if not self.navd_active:
                #print("clear path_points: navd_active: ", self.navd_active)
                self.navi_points = []
                self.navi_points_active = False

          except OSError as e:
            if e.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN):
              if self.connection:
                self.connection.close()
              self.connection = None
              self.remote_addr = None
              self.ip_address = "0.0.0.0"
              next_broadcast_time = now + BROADCAST_NETWORK_ERROR_RETRY_INTERVAL
              if now - last_network_error_log_time >= BROADCAST_NETWORK_ERROR_LOG_INTERVAL:
                print(f"[carrot_man] broadcast skipped: {e}")
                last_network_error_log_time = now
            else:
              if self.connection:
                self.connection.close()
              self.connection = None
              print(f"##### broadcast_error...: {e}")
              traceback.print_exc()
          except Exception as e:
            if self.connection:
              self.connection.close()
            self.connection = None
            print(f"##### broadcast_error...: {e}")
            traceback.print_exc()

        rk.keep_time()
        frame += 1
      except Exception as e:
        print(f"broadcast_version_info error...: {e}")
        traceback.print_exc()
        time.sleep(1)


  def _update_carrot_navi_route(self, navi: CarrotNaviControl | None, force: bool = False):
    previous_session = self.carrot_navi_route_session_id
    route_owned = self.carrot_navi_route_owned
    if navi is None:
      if not previous_session:
        return
      self.carrot_navi_route_session_id = ""
      self.carrot_navi_route_sequence = -1
      if force:
        self.carrot_navi_route_owned = False
        return
      if not route_owned:
        return
      points = ()
    else:
      new_session = navi.session_id != previous_session
      route = navi.route
      if not force and not new_session and route.sequence == self.carrot_navi_route_sequence:
        route_available = route.present and bool(route.polyline)
        if not route_available or self.navi_points_active or self.params.get_bool("IsOffroad"):  # 本仓 manager 维护 IsOffroad
          return
      self.carrot_navi_route_session_id = navi.session_id
      self.carrot_navi_route_sequence = route.sequence
      points = route.polyline if route.present else ()
      if not points and force:
        self.carrot_navi_route_owned = False
        return
      if not points and not route_owned:
        return

    coords = [
      {"latitude": latitude, "longitude": longitude}
      for latitude, longitude in points
    ]
    self.navi_points = [(point["longitude"], point["latitude"]) for point in coords]
    self.navi_points_start_index = 0
    self.navi_points_active = bool(self.navi_points)
    self.carrot_navi_route_owned = self.navi_points_active
    # Keep the existing route consumer alive without asking navd to calculate a
    # different route from the app's destination.
    self.navd_active = self.navi_points_active
    self.send_routes(coords)

  def carrot_navi_route(self):

    if self.carrot_serv.active_carrot > 1:
      if False and self.navd_active:  # mabox always active
        self.navd_active = False
        self.params.remove("NavDestination")
    is_onroad = not self.params.get_bool("IsOffroad")
    if not is_onroad or not self.navi_points_active or not SHAPELY_AVAILABLE or (self.carrot_serv.active_carrot <= 1 and not self.navd_active):
      #print(f"navi_points_active: {self.navi_points_active}, active_carrot: {self.carrot_serv.active_carrot}")
      if self.navi_points_active:
        print("navi_points_active: ", self.navi_points_active, "active_carrot: ", self.carrot_serv.active_carrot, "navd_active: ", self.navd_active)
        #haversine_cache.clear()
        #curvature_cache.clear()
        self.navi_points = []
        self.navi_points_active = False
        if self.active_carrot_last > 1:
          #self.params.remove("NavDestination")
          pass
      self.active_carrot_last = self.carrot_serv.active_carrot
      return [],[],300

    current_position = (self.carrot_serv.vpPosPointLon, self.carrot_serv.vpPosPointLat)
    heading_deg = self.carrot_serv.bearing

    distance_interval = 10.0
    out_speed = 300
    path, self.navi_points_start_index, start_point = get_path_after_distance(self.navi_points_start_index, self.navi_points, current_position, 300)
    relative_coords = []
    if path:
        #relative_coords = gps_to_relative_xy(path, current_position, heading_deg)
        relative_coords = gps_to_relative_xy(path, start_point, heading_deg)
        # Resample relative_coords at 5m intervals using LineString
        line = LineString(relative_coords)
        resampled_points = []
        resampled_distances = []
        current_distance = 0
        while current_distance <= line.length:
            point = line.interpolate(current_distance)
            resampled_points.append((point.x, point.y))
            resampled_distances.append(current_distance)
            current_distance += distance_interval

        curvatures = []
        distances = []
        distance = 10.0
        sample = 4
        if len(resampled_points) >= sample * 2 + 1:
            # Calculate curvatures and speeds based on curvature
            speeds = []
            for i in range(len(resampled_points) - sample * 2):
                distance += distance_interval
                p1, p2, p3 = resampled_points[i], resampled_points[i + sample], resampled_points[i + sample * 2]
                curvature = calculate_curvature(p1, p2, p3)
                curvatures.append(curvature)
                # curvature (1/m) -> target speed (kph): v=sqrt(a_lat/r) from
                # curve_speed.TARGET_LAT_ACCEL, in kph, capped at NO_LIMIT_KPH.
                # The ported V_CURVE_LOOKUP_* table was never defined.
                speed = min(NO_LIMIT_KPH, math.sqrt(TARGET_LAT_ACCEL / max(abs(curvature), 1e-6)) * 3.6)
                if abs(curvature) < 0.02:
                  speed = max(speed, self.carrot_serv.nRoadLimitSpeed)
                speeds.append(speed)
                distances.append(distance)
            #print(f"curvatures= {[round(s, 4) for s in curvatures]}")
            #print(f"speeds= {[round(s, 1) for s in speeds]}")
            # Apply acceleration limits in reverse to adjust speeds
            accel_limit = self.carrot_serv.autoNaviSpeedDecelRate # m/s^2
            accel_limit_kmh = accel_limit * 3.6  # Convert to km/h per second
            out_speeds = [0] * len(speeds)
            out_speeds[-1] = speeds[-1]  # Set the last speed as the initial value
            v_ego_kph = self.sm['carState'].vEgo * 3.6

            time_delay = self.carrot_serv.autoNaviSpeedCtrlEnd
            time_wait = 0
            for i in range(len(speeds) - 2, -1, -1):
                target_speed = speeds[i]
                next_out_speed = out_speeds[i + 1]

                if target_speed < next_out_speed:
                  time_delay = max(0, ((v_ego_kph - target_speed) / accel_limit_kmh))
                  time_wait = - time_delay

                # Calculate time interval for the current segment based on speed
                time_interval = distance_interval / (next_out_speed / 3.6) if next_out_speed > 0 else 0

                time_apply = min(time_interval, max(0, time_interval + time_wait))

                # Calculate maximum allowed speed with acceleration limit
                max_allowed_speed = next_out_speed + (accel_limit_kmh * time_apply)
                adjusted_speed = min(target_speed, max_allowed_speed)

                #time_wait += time_interval
                time_wait += min(2.0, time_interval)

                out_speeds[i] = adjusted_speed

            #distance_advance = self.sm['carState'].vEgo * 3.0  # Advance distance by 3.0 seconds
            #out_speed = interp(distance_advance, distances, out_speeds)
            out_speed = out_speeds[0]
            #print(f"out_speeds= {[round(s, 1) for s in out_speeds]}")
    else:
        resampled_points = []
        resampled_distances = []
        curvatures = []
        speeds = []
        distances = []
        #self.params.remove("NavDestination")

    return resampled_points, resampled_distances, out_speed #speeds, distances


  def make_send_message(self):
    msg = {}
    msg['Carrot2'] = self.params.get("Version")
    isOnroad = not self.params.get_bool("IsOffroad")  # 本仓 manager 维护 IsOffroad
    msg['IsOnroad'] = isOnroad
    msg['CarrotRouteActive'] = self.navi_points_active
    msg['ip'] = self.ip_address
    msg['port'] = self.carrot_man_port
    msg['navi_debug'] = 0
    msg['navi_http_port'] = self.carrot_navi_http_port
    self.controls_active = False
    self.xState = 0
    self.trafficState = 0
    v_ego_kph = 0
    v_cruise_kph = 0
    carcruiseSpeed = 0
    if not isOnroad:
      self.xState = 0
      self.trafficState = 0
    else:
      if self.sm.alive['carState']:
        carState = self.sm['carState']
        v_ego_kph = int(carState.vEgoCluster * 3.6 + 0.5)
        v_cruise_kph = carState.vCruise
        carcruiseSpeed = carState.cruiseState.speed * 3.6
      if self.sm.alive['selfdriveState']:
        selfdrive = self.sm['selfdriveState']
        self.controls_active = selfdrive.active
      # 本仓 LongitudinalPlan 无 carrot 的 xState/trafficState 扩展：trafficState 取 carrot_serv 状态机
      self.trafficState = self.carrot_serv.traffic_state

    msg['v_cruise_kph'] = v_cruise_kph
    msg['carcruiseSpeed'] = carcruiseSpeed
    msg['v_ego_kph'] = v_ego_kph
    msg['tbt_dist'] = self.carrot_serv.xDistToTurn
    msg['sdi_dist'] = self.carrot_serv.xSpdDist
    msg['active'] = self.controls_active
    msg['xState'] = self.xState
    msg['trafficState'] = self.trafficState
    return json.dumps(msg)

  def send_routes(self, coords, from_navd=False):
    original_count = len(coords)
    coords = limit_route_points(coords, NAVI_ROUTE_MAX_POINTS)
    if original_count > len(coords):
      print(f"Route points limited: {original_count} -> {len(coords)}")

    if from_navd:
      if len(coords) > 0:
        self.navi_points = [(c.longitude, c.latitude) for c in coords]
        self.navi_points_start_index = 0
        self.navi_points_active = True
        print("Received points from navd:", len(self.navi_points))
        self.navd_active = True

        # 경로수신 -> carrotman active되고 약간의 시간지연이 발생함..
        if not from_navd:
          self.carrot_serv.active_count = 80
          self.carrot_serv.active_sdi_count = self.carrot_serv.active_sdi_count_max
          self.carrot_serv.active_carrot = 2

        coords = [{"latitude": c.latitude, "longitude": c.longitude} for c in coords]
        #print("navdNaviPoints=", self.navi_points)
      else:
        print("Received points from navd: 0")
        self.navi_points = []
        self.navi_points_start_index = 0
        self.navi_points_active = False
        self.navd_active = False

    msg = messaging.new_message('navRoute', valid=True)
    msg.navRoute.coordinates = coords
    self.pm.send('navRoute', msg)

  def carrot_curve_speed_params(self):
    self.autoCurveSpeedFactor = get_int_param(self.params, "AutoCurveSpeedFactor", 120) * 0.01

  def carrot_curve_speed(self, sm):
    self.carrot_curve_speed_params()
    if not all(sm.alive[name] and sm.valid[name] for name in ('carState', 'modelV2')):
      return self.vision_curve_speed.update(None, time.monotonic())
    return self.vturn_speed(sm['carState'], sm)

  def vturn_speed(self, CS, sm):
    # 本仓 CarState 无 vCluRatio（cluster 车速比）：恒 1.0
    result = curve_speed(sm['modelV2'], CS.vEgo, self.autoCurveSpeedFactor,
                         self.carrot_serv.autoCurveSpeedLowerLimit,
                         speed_ratio=1.0, a_ego=CS.aEgo)
    return self.vision_curve_speed.update(result, time.monotonic(), model_time=sm.logMonoTime['modelV2'])


def main():
  try:
    set_core_affinity([0, 1, 2, 3])
  except Exception:
    print("[carrot_man] failed to set core affinity")

  print("CarrotManager Started")
  carrot_man = CarrotMan()
  print(f"CarrotMan {carrot_man}")
  # 20Hz 主循环（原 broadcast_version_info 线程）：SubMaster 消费 + carrot_serv 聚合发布 + 7705 发现广播
  carrot_man.broadcast_version_info()


if __name__ == "__main__":
  main()

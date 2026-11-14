"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import random
import time

from openpilot.common.parameterized import parameterized

from openpilot.cereal import custom
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit import LIMIT_MAX_MAP_DATA_AGE, LIMIT_MAX_CARROT_NAVI_AGE

from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_resolver import SpeedLimitResolver, ALL_SOURCES
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.common import Policy
from openpilot.common.test import OpenpilotTestCase

SpeedLimitSource = custom.LongitudinalPlanSP.SpeedLimit.Source


def create_mock(properties, mocker):
  mock = mocker.MagicMock()
  for _property, value in properties.items():
    setattr(mock, _property, value)
  return mock


def setup_carrot_sm_mock(mocker, limit_kph=80.0, *, seen=True, alive=True, valid=True,
                         recv_age=0.0, connected=True, present=True, road_limit_valid=True):
  """carrotNavi SubMaster 契约替身：新鲜度/连接/数据有效性逐项可控。

  复用 setup_sm_mock 提供其余服务（resolver.update 会同时读 carStateSP /
  liveMapDataSP / gpsLocation），仅替换 carrotNavi 键与新鲜度元数据。
  """
  navi = custom.CarrotNaviState.new_message()
  navi.connected = connected
  navi.speed.meta.present = present
  navi.speed.roadLimitValid = road_limit_valid
  navi.speed.roadLimitKph = int(limit_kph)
  sm_mock = setup_sm_mock(mocker)
  base_getitem = sm_mock.__getitem__.side_effect
  sm_mock.__getitem__.side_effect = lambda key: navi if key == 'carrotNavi' else base_getitem(key)
  sm_mock.seen = {'carrotNavi': seen}
  sm_mock.alive = {'carrotNavi': alive}
  sm_mock.valid = {'carrotNavi': valid}
  sm_mock.recv_time = {'carrotNavi': time.monotonic() - recv_age}
  return sm_mock


def setup_sm_mock(mocker):
  cruise_speed_limit = random.uniform(0, 120)
  live_map_data_limit = random.uniform(0, 120)

  car_state = create_mock({
    'gasPressed': False,
    'brakePressed': False,
    'standstill': False,
  }, mocker)
  car_state_sp = create_mock({
    'speedLimit': cruise_speed_limit,
  }, mocker)
  live_map_data = create_mock({
    'speedLimit': live_map_data_limit,
    'speedLimitValid': True,
    'speedLimitAhead': 0.,
    'speedLimitAheadValid': 0.,
    'speedLimitAheadDistance': 0.,
  }, mocker)
  gps_data = create_mock({
    'unixTimestampMillis': time.monotonic() * 1e3,
  }, mocker)
  sm_mock = mocker.MagicMock()
  sm_mock.__getitem__.side_effect = lambda key: {
    'carState': car_state,
    'liveMapDataSP': live_map_data,
    'carStateSP': car_state_sp,
    'gpsLocation': gps_data,
  }[key]
  return sm_mock


parametrized_policies = parameterized.expand(
  [
    (Policy.car_state_only, 'carStateSP', SpeedLimitSource.car),
    (Policy.car_state_priority, 'carStateSP', SpeedLimitSource.car),
    (Policy.map_data_only, 'liveMapDataSP', SpeedLimitSource.map),
    (Policy.map_data_priority, 'liveMapDataSP', SpeedLimitSource.map),
  ],
  names=["policy", "sm_key", "function_key"]
)


def resolver_class():
  return SpeedLimitResolver


class TestSpeedLimitResolverValidation(OpenpilotTestCase):

  @parameterized.expand(list(Policy), names=["policy"])
  def test_initial_state(self, resolver_class, policy):
    resolver = resolver_class()
    resolver.policy = policy
    for source in ALL_SOURCES:
      if source in resolver.limit_solutions:
        assert resolver.limit_solutions[source] == 0.
        assert resolver.distance_solutions[source] == 0.

  @parametrized_policies
  def test_resolver(self, resolver_class, policy, sm_key, function_key, mocker):
    resolver = resolver_class()
    resolver.policy = policy
    sm_mock = setup_sm_mock(mocker)
    source_speed_limit = sm_mock[sm_key].speedLimit

    # Assert the resolver
    resolver.update(source_speed_limit, sm_mock)
    assert resolver.speed_limit == source_speed_limit
    assert resolver.source == ALL_SOURCES[function_key]

  def test_resolver_combined(self, resolver_class, mocker):
    resolver = resolver_class()
    resolver.policy = Policy.combined
    sm_mock = setup_sm_mock(mocker)
    socket_to_source = {'carStateSP': SpeedLimitSource.car, 'liveMapDataSP': SpeedLimitSource.map}
    minimum_key, minimum_speed_limit = min(
      ((key, sm_mock[key].speedLimit) for key in
       socket_to_source.keys()), key=lambda x: x[1])

    # Assert the resolver
    resolver.update(minimum_speed_limit, sm_mock)
    assert resolver.speed_limit == minimum_speed_limit
    assert resolver.source == socket_to_source[minimum_key]

  @parametrized_policies
  def test_parser(self, resolver_class, policy, sm_key, function_key, mocker):
    resolver = resolver_class()
    resolver.policy = policy
    sm_mock = setup_sm_mock(mocker)
    source_speed_limit = sm_mock[sm_key].speedLimit

    # Assert the parsing
    resolver.update(source_speed_limit, sm_mock)
    assert resolver.limit_solutions[ALL_SOURCES[function_key]] == source_speed_limit
    assert resolver.distance_solutions[ALL_SOURCES[function_key]] == 0.

  @parameterized.expand(list(Policy), names=["policy"])
  def test_resolve_interaction_in_update(self, resolver_class, policy, mocker):
    v_ego = 50
    resolver = resolver_class()
    resolver.policy = policy

    sm_mock = setup_sm_mock(mocker)
    resolver.update(v_ego, sm_mock)

    # After resolution
    assert resolver.speed_limit is not None
    assert resolver.distance is not None
    assert resolver.source is not None

  @parameterized.expand(list(Policy), names=["policy"])
  def test_old_map_data_ignored(self, resolver_class, policy, mocker):
    resolver = resolver_class()
    resolver.policy = policy
    sm_mock = mocker.MagicMock()
    sm_mock['gpsLocation'].unixTimestampMillis = (time.monotonic() - 2 * LIMIT_MAX_MAP_DATA_AGE) * 1e3
    resolver._get_from_map_data(sm_mock)
    assert resolver.limit_solutions[SpeedLimitSource.map] == 0.
    assert resolver.distance_solutions[SpeedLimitSource.map] == 0.


class TestCarrotNaviSource(OpenpilotTestCase):
  """carrot 源（#3）：carrotNavi.speed.roadLimitKph 接入 SLC，新鲜度 fail-open。"""

  def test_fresh_carrot_limit_resolves(self, mocker):
    # map_data_only 策略：carrot 新鲜有效 → carrot 优先于 OSM map
    resolver = SpeedLimitResolver()
    resolver.policy = Policy.map_data_only
    sm_mock = setup_carrot_sm_mock(mocker, limit_kph=80.0)
    resolver.update(20.0, sm_mock)
    assert resolver.source == SpeedLimitSource.carrot
    assert resolver.speed_limit == 80.0 * (1 / 3.6)
    assert resolver.distance_solutions[SpeedLimitSource.carrot] == 0.

  def test_stale_carrot_falls_back_to_map(self, mocker):
    # carrotNavi 1.5s 无新帧（掉线）→ carrot 置 0，map_data_only 回落 OSM map
    resolver = SpeedLimitResolver()
    resolver.policy = Policy.map_data_only
    sm_mock = setup_carrot_sm_mock(mocker, recv_age=LIMIT_MAX_CARROT_NAVI_AGE + 0.1)
    resolver.update(20.0, sm_mock)
    assert resolver.limit_solutions[SpeedLimitSource.carrot] == 0.
    assert resolver.source == SpeedLimitSource.map

  def test_disconnected_carrot_ignored(self, mocker):
    # carrotmate 断连但 publisher 心跳仍发：connected 双保险拦截
    resolver = SpeedLimitResolver()
    sm_mock = setup_carrot_sm_mock(mocker, connected=False)
    resolver._get_from_carrot_navi(sm_mock)
    assert resolver.limit_solutions[SpeedLimitSource.carrot] == 0.

  @parameterized.expand([
    ("not_present", dict(present=False)),
    ("limit_invalid", dict(road_limit_valid=False)),
    ("limit_zero", dict(limit_kph=0)),
    ("limit_over_max", dict(limit_kph=250)),
  ], names=["case", "kwargs"])
  def test_invalid_carrot_data_ignored(self, case, kwargs, mocker):
    resolver = SpeedLimitResolver()
    sm_mock = setup_carrot_sm_mock(mocker, **kwargs)
    resolver._get_from_carrot_navi(sm_mock)
    assert resolver.limit_solutions[SpeedLimitSource.carrot] == 0.

  def test_car_state_only_ignores_carrot(self, mocker):
    # car_state_only 策略不吃 carrot 源（尊重用户策略意图）
    resolver = SpeedLimitResolver()
    resolver.policy = Policy.car_state_only
    sm_mock = setup_carrot_sm_mock(mocker, limit_kph=80.0)
    sm_mock['carStateSP'].speedLimit = 60.0
    resolver.update(20.0, sm_mock)
    assert resolver.source == SpeedLimitSource.car
    assert resolver.speed_limit == 60.0

  def test_carrot_absent_service_ignored(self, mocker):
    # plannerd 未收到 carrotNavi（服务从未上线）→ fail-open
    resolver = SpeedLimitResolver()
    sm_mock = setup_carrot_sm_mock(mocker, seen=False, alive=False, valid=False)
    resolver._get_from_carrot_navi(sm_mock)
    assert resolver.limit_solutions[SpeedLimitSource.carrot] == 0.

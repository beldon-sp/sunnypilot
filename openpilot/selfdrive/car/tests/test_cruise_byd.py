import numpy as np

from openpilot.common.test import OpenpilotTestCase
from openpilot.cereal import custom, log
from opendbc.car.structs import car
from openpilot.common.constants import CV
from openpilot.selfdrive.car.cruise import VCruiseHelper, V_CRUISE_UNSET


def make_v_cruise_helper(brand):
  CP = car.CarParams(brand=brand, pcmCruise=True)
  CP_SP = custom.CarParamsSP(pcmCruiseSpeed=True)
  return VCruiseHelper(CP, CP_SP)


def run_update(helper, available, speed_kph, cluster_kph):
  CS = car.CarState(
    gearShifter=car.CarState.GearShifter.drive,
    vEgoCluster=40 * CV.KPH_TO_MS,
    cruiseState={"available": available, "speed": speed_kph * CV.KPH_TO_MS,
                 "speedCluster": cluster_kph * CV.KPH_TO_MS},
  )
  helper.update_v_cruise(CS, enabled=True, is_metric=True)


class TestVCruiseByd(OpenpilotTestCase):
  def setup_byd(self, prev_kph):
    helper = make_v_cruise_helper("byd")
    helper.v_cruise_kph = prev_kph
    helper.v_cruise_cluster_kph = prev_kph
    return helper

  def test_byd_follows_stalk_set_speed(self):
    # BYD (Song Plus DM-i) is pcmCruise with no buttonEvents in carstate -
    # nothing in the stock branch can raise a virtual target (real-car bug
    # 2026-10-05: Experimental + AlphaLong never accelerated, pinned at ~35).
    # The branch must take the stalk's radar SetSpeed instead.
    helper = self.setup_byd(prev_kph=30)
    run_update(helper, available=True, speed_kph=72, cluster_kph=72)
    self.assertAlmostEqual(helper.v_cruise_kph, 72, places=4)
    self.assertAlmostEqual(helper.v_cruise_cluster_kph, 72, places=4)

  def test_byd_holds_last_target_through_set_speed_zero(self):
    # The radar's own frames can carry SetSpeed==0 for the first 1-2 s of a
    # session (byd carcontroller, routes 31/2d); sync must not chase the blip
    # to V_CRUISE_UNSET/0 (that would command a stop).
    helper = self.setup_byd(prev_kph=50)
    run_update(helper, available=True, speed_kph=0, cluster_kph=0)
    self.assertEqual(helper.v_cruise_kph, 50)
    self.assertEqual(helper.v_cruise_cluster_kph, 50)

  def test_byd_sync_independent_of_pcm_cruise_speed_param(self):
    # the brand gate fires regardless of CP_SP.pcmCruiseSpeed, so a fresh
    # device works without user configuration (with the param False and the
    # enable latch down, the sync branch is still the byd stalk-owned one)
    CP = car.CarParams(brand="byd", pcmCruise=True)
    CP_SP = custom.CarParamsSP(pcmCruiseSpeed=False)
    helper = VCruiseHelper(CP, CP_SP)
    helper.v_cruise_kph = 30
    helper.v_cruise_cluster_kph = 30
    run_update(helper, available=True, speed_kph=88, cluster_kph=88)
    self.assertAlmostEqual(helper.v_cruise_kph, 88, places=4)
    self.assertAlmostEqual(helper.v_cruise_cluster_kph, 88, places=4)

  def test_other_brands_keep_pcm_sync_paths(self):
    # stock pcm brands: unchanged sync, including the SetSpeed==0 -> UNSET map
    helper = make_v_cruise_helper("toyota")
    run_update(helper, available=True, speed_kph=60, cluster_kph=60)
    self.assertAlmostEqual(helper.v_cruise_kph, 60, places=4)

    helper = make_v_cruise_helper("toyota")
    helper.v_cruise_kph = 50
    run_update(helper, available=True, speed_kph=0, cluster_kph=0)
    self.assertEqual(helper.v_cruise_kph, V_CRUISE_UNSET)

  def test_byd_unavailable_keeps_default_unset_branch(self):
    # ACC main off: available==False path untouched - V_CRUISE_UNSET (this
    # fork's convention; main off has no meaningful SetSpeed and OP is
    # disengaged by the latch drop)
    helper = self.setup_byd(prev_kph=90)
    run_update(helper, available=False, speed_kph=0, cluster_kph=0)
    self.assertEqual(helper.v_cruise_kph, V_CRUISE_UNSET)


if __name__ == "__main__":
  import unittest
  unittest.main()

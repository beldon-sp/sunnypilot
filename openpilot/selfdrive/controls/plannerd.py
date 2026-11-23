#!/usr/bin/env python3
from openpilot.cereal import custom
from opendbc.car.structs import car
from openpilot.common.gps import get_gps_location_service
from openpilot.common.params import Params
from openpilot.common.realtime import Priority, config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.controls.lib.ldw import LaneDepartureWarning
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
import openpilot.cereal.messaging as messaging


def main():
  config_realtime_process(5, Priority.CTRL_LOW)

  cloudlog.info("plannerd is waiting for CarParams")
  params = Params()
  CP = messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams)
  cloudlog.info("plannerd got CarParams: %s", CP.brand)

  cloudlog.info("plannerd is waiting for CarParamsSP")
  CP_SP = messaging.log_from_bytes(params.get("CarParamsSP", block=True), custom.CarParamsSP)
  cloudlog.info("plannerd got CarParamsSP")

  gps_location_service = get_gps_location_service(params)
  # carrotNavi 是可选的导航增强服务（carrotmate，2Hz，仅导航中稳定发布）。车静止或
  # carrotmate 未连接时其频率不达标；若参与 all_checks()，会把本进程发布的
  # longitudinalPlan / driverAssistance 的 valid 置为 False，级联触发 selfdrived 的
  # commIssue 并阻止 engagement（表现为车机"自适应功能受限"）。导航可用性由各 carrot
  # 消费者（SLC resolver / navi_alerts / navi_lane_change_gate）自行判定，它们读的是
  # sm.seen/alive/valid/recv_time 原始字段，不受此 ignore 影响，故豁免是安全的。
  ignore_services = ["liveMapDataSP", "carStateSP", "selfdriveStateSP", gps_location_service, "carrotNavi"]

  ldw = LaneDepartureWarning()
  longitudinal_planner = LongitudinalPlanner(CP, CP_SP)
  pm = messaging.PubMaster(['longitudinalPlan', 'driverAssistance', 'longitudinalPlanSP'])
  sm = messaging.SubMaster(['carControl', 'carState', 'controlsState', 'radarState', 'modelV2', 'selfdriveState',
                            'liveMapDataSP', 'carStateSP', 'selfdriveStateSP', 'carrotMan', 'carrotNavi', gps_location_service],
                           poll='modelV2', ignore_alive=ignore_services, ignore_avg_freq=ignore_services, ignore_valid=ignore_services)

  while True:
    sm.update()
    # SLA gate（design D2）：carrot 权威期禁止 SLA 消费按键确认（改巡航目标）；
    # 退出后恢复调用，SLA 以当前限速重新武装
    if not longitudinal_planner.carrot.mode_active:
      longitudinal_planner.sla.update_buttons(sm['selfdriveStateSP'].buttonsReleaseToggle)
    if sm.updated['modelV2']:
      longitudinal_planner.update(sm)
      longitudinal_planner.publish(sm, pm)

      ldw.update(sm.frame, sm['modelV2'], sm['carState'], sm['carControl'])
      msg = messaging.new_message('driverAssistance')
      msg.valid = sm.all_checks()
      msg.driverAssistance.leftLaneDeparture = ldw.left
      msg.driverAssistance.rightLaneDeparture = ldw.right
      pm.send('driverAssistance', msg)


if __name__ == "__main__":
  main()

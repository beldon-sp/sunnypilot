# locationd 鲁棒性修复：mouxancnb 版 vs 本分支现状

**日期**: 2026-11-28
**主题**: 消灭"行驶中途误报 SOFT_DISABLE（立即接管）"这一类故障。源库 4 个修复全部有 rlog 实证事故链，本分支逐项核对后：**2 个直接原样移植，1 个需要简化版自建，1 个本地用不上**。

## ✅ 实施状态（2026-11-28，branch `byd-tici`）

| # | 状态 | commit | 内容 |
|---|---|---|---|
| ④ | ✅ 已实施 | `033ab61a8` | managerState 无条件 ignore（`selfdrived.py`）+ SIGKILL 后 join 限 5s、D 态延迟记账（`system/manager/process.py`），逐行对应 §3-④ |
| ③ | ✅ 已实施 | `188f2f42b` | 简化版 grace：`selfdrived.py` commIssue 分支加 `comm_issue_valid_run` 分类计数，all_alive/all_freq_ok 失败立即 raise，仅 valid 失败容忍 `int(1./DT_CTRL)`（100 帧 @100Hz）后 raise；恢复自动清零 |
| ① | ✅ 已实施 | `7aed67df2` | 新增 `openpilot/selfdrive/locationd/gyro_crosscheck.py`（纯函数，含 `YAWRATE_CROSSCHECK_MIN_SPEED=1.0`）+ `locationd.py` 接入 + 单测 `test/test_gyro_crosscheck.py`（5 用例，PC 可跑，capnp-free） |
| ② | ➖ 按计划不搬 | — | 本地预算架构不同，等价效果由 ③ 覆盖 |

**验证情况**：
- `py_compile` 三个改动文件通过；`test_gyro_crosscheck.py` 5 passed。
- `system/manager/test` 7 passed + 1 failed（`test_set_params_with_default_value`，**干净树上同样失败，既有问题，与本改动无关**）。
- `test_locationd_scenarios.py` 在本机干净树和改动后均挂起（需 scons 编译环境，环境限制非改动引入）。
- **未完成**：①③ 属驾驶可用性，按 No-Go 纪律还需 replay 对比通过后才上实车，且先短途。
- 备注：`sunnypilot/selfdrive/locationd/locationd.cc` 存在同款交叉校验，但设备实际跑的是 Python 版（`process_config.py:148`），C++ 版未被启动，未改动。

---

## 1. 源修复盘点（按事故因果链）

### ① 停车打轮 → gyro 交叉校验误杀（`3665198b6`）
- **事故**：停车场打方向盘，车身被轮胎拖拽扭转，IMU 诚实报告 |gyro z|~0.025 rad/s，而 cameraOdometry 报 yaw=0 且 std 极小 → 每帧都判 gyro INPUT_INVALID → sensor budget（上限 10，~10s 衰减）耗尽 → inputsOK=false → locationdTemporaryError（SOFT_DISABLE）。实证路线：2.2s 内 12 次触发，全部 vEgo=0.00、~60° 打轮，err 0.033 vs 阈值 0.030 rad/s。
- **修复**：vEgo < 1.0 m/s 跳过交叉校验（ROTATION_SANITY_CHECK=10 rad/s 幅值门仍在，坏 gyro 溜不进来）；行驶中校验不变。抽成 `gyro_crosscheck.py` 纯函数模块 + 单测（capnp-free，PC 可跑）。

### ② 孤立单帧 camodo 丢帧 → 预算误扣（`495d95b1d`）
- **事故**：modeld 丢一帧 VIPC → cameraOdometry valid=False 一帧，花掉预算。两次孤立丢帧（间隔 6.4s，60km/h 直行）把计数推到 1.83 ≥ 1.5 → inputsOK=false 锁 7.0s → 误报。
- **修复**：连续 ≥2 帧无效才一次性收整段费用（镜像 TIMING grace 思路）→ 真断粮立即锁，孤立抖动免费。
- **注意**：这是在**新版预算架构**（`update_observation_invalid` / TIMING vs INPUT 分类）上的补丁。

### ③ commIssue valid-only grace 5帧 → 1s（`e50bd68aa`）
- **事故**：radard 350ms 调度停顿 → 一帧 valid=False radarState → plannerd 级联镜像 → selfdrived 端联合无效窗口 88ms（~9 帧 @100Hz）> 旧 5 帧宽限 → 误发 commIssue SOFT_DISABLE，行驶中脱开 ~2s。
- **修复**：valid-only 失败路径要求持续 1s（100 帧）才 raise；dead/slow publisher 路径（all_alive/all_freq_ok）保持立即 raise。实现在 `sunnypilot/common/safety_guards.py` 的 `CommIssueGrace`。

### ④ manager 卡死连锁 → commIssue 19s（`71488a1a7`）
- **事故**（C4 实录）：sunnylink webrtc 中途拉起 stream_encoderd，与录像 encoderd 抢同一个 qcom_vidc 视频节点 → 双双刷 poll timeout → manager stop() 时子进程无视 SIGINT 5s、SIGKILL 后又滞留内核 D 态 ~2s → **stop() 里无界 proc.join() 把 managerd 主循环冻 7.6s** → managerState stale，是 selfdrived 唯一没 ignore 的喂源 → commIssue SOFT_DISABLE 19s（658 帧）。
- **修复两层**：
  - selfdrived：managerState 不参与 alive/freq/valid gating（纯监督数据；manager 真死会由控制链路喂源死亡暴露，死进程仍由 processNotRunning 按消息内容抓）；
  - manager `stop()`：SIGKILL 后 join 限 5s，D 态未 reap 时延迟记账，不再阻塞主循环。

---

## 2. 本分支现状核对（逐项已验证）

| # | 源修复 | 本地现状 | 判断 |
|---|---|---|---|
| ① | 交叉校验低速豁免 | `locationd.py:134-140` **内联同款交叉校验，无低速豁免**。变量一一对应：`meas[2]-bias[2]` vs `camodo_yawrate_distribution`（mean/std）+ `self.car_speed`（:149 已维护） | ✅ **直接移植，价值最高**。bug 在本地真实存在（你的车也一样：泊车场景 LKAS 附加使用时同样中招） |
| ② | camodo 单帧豁免 | 本地**预算架构不同**：invalid 消息被 `if valid:` 直接跳过（:314），不进预算；那一帧的 invalid 通过 `sm.all_valid()` 逐帧暴露给 deviceMotion → 交给 selfdrived 处理 | ⛔ **用不上**：run-grace 补的是新版预算机制，本地的等价效果由 ③ 的 grace 实现 |
| ③ | commIssue 1s grace | 本地**无任何 grace 基础设施**：`selfdrived.py:510` 首帧 all_checks 失败即 add commIssue；本地也没有 `sunnypilot/common/safety_guards.py`（common/ 下只有 transformations + version.h） | ⚠️ **不能照搬**（依赖新版 CommIssueGrace 基建），需**简化版自建**（见下） |
| ④ | managerState ignore + stop() join 限时 | 本地 `selfdrived.py:116` managerState 仅 SIMULATION 下 ignore（硬件上参与 gating）；`process.py:100` SIGKILL 后 `self.proc.join()` **无界** —— 同款冻结 bug 原样存在 | ✅ **两处原样移植**，补丁上下文与本地代码逐行吻合 |

**本地特有加分项**：你的设备 `DISABLE_DRIVER` 已把 driverMonitoringState/cabinCameraState 加入 ignore（selfdrived.py:121-123），源库 c55a2b16c"停用 DMA 参与 commIssue gating"的诉求本地已覆盖。

---

## 3. 建议吸收清单

### ① gyro 交叉校验低速豁免（~30 行 + 单测，直接抄）
1. 新增 `openpilot/selfdrive/locationd/gyro_crosscheck.py`：原样移植（纯函数，含 `YAWRATE_CROSSCHECK_MIN_SPEED = 1.0`）。
2. `locationd.py:135-140` 改为调用 helper：
   ```python
   gyro_valid = gyro_yawrate_crosscheck_valid(
     meas[2], gyro_bias[2],
     self.camodo_yawrate_distribution[0], self.camodo_yawrate_distribution[1],
     self.car_speed)
   ```
   注意本地 `meas` 已是 `[-v2,-v1,-v0]` 后的值，`meas[2]` 即设备系 yaw，与源库语义一致。
3. 单测照抄（PC 可跑）。与 virtual sensord 移植方案的 Stage A/B **天然协同**：合成 gyro 同样要过这道校验，低速豁免对它同样生效。

### ④ manager 卡死连锁（两处小补丁，直接抄）
1. `selfdrived.py`：把 `managerState` 从 SIMULATION 条件块移入无条件 ignore（与 `alertDebug` 同级）。安全性依据：死进程检测走 `:478-483` 的 processNotRunning（消息内容），不依赖喂源活性。
2. `system/manager/process.py:100`：`self.proc.join()` → `join_process(self.proc, 5)` + exitcode 为 None 时 warning + 延迟记账（源补丁逐行吻合，本地 :53 已有 join_process 可用）。

### ③ commIssue 简化版 grace（自建，~20 行，放最后）
不搬 safety_guards.py 基建，在本地 `selfdrived.py:510` 分支加分类计数：
```python
# __init__: self.comm_issue_valid_run = 0
if not self.sm.all_checks() and no_system_errors and not big_model_settling:
  if not self.sm.all_alive():          # 死进程 → 真故障，立即
    self.comm_issue_valid_run = 0
    self.events.add(EventName.commIssue)
  elif not self.sm.all_freq_ok():      # 慢喂源 → 真故障，立即
    self.comm_issue_valid_run = 0
    self.events.add(EventName.commIssueAvgFreq)
  else:                                # 仅 valid 失败 → 抖动容忍 1s
    self.comm_issue_valid_run += 1
    if self.comm_issue_valid_run >= 100:  # 1 s @100Hz
      self.events.add(EventName.commIssue)
else:
  self.comm_issue_valid_run = 0
```
语义与源库一致：只有"数据还在来、频率正常、但 valid=False"的抖动（radard 停顿→plannerd 级联、单帧 camodo 丢帧）被吸收；真故障 1s 内必触发。事件为逐帧重建，恢复自动清除，无需额外回滚逻辑。

---

## 4. 实施顺序与验证

1. **④（最低风险）**：不改驾驶逻辑，只防冻结连锁。改完设备上手动 kill -9 一个进程验证 manager 不冻结。
2. **①（最高价值）**：PC 单测 → replay 找一段带低速打轮的路线（或停车场短途）验证计数器不再上涨。
3. **③**：replay 模拟单帧 invalid（可用 cabana 删帧），确认 88ms 级抖动不再触发、真断源 1s 内触发。
4. ①③ 都属驾驶可用性（SOFT_DISABLE 触发面），按 No-Go 纪律：replay 对比通过后才上实车，且先短途。

## 5. 明确不搬的部分
- ② 的 `update_camodo_drop`：本地预算架构不同，硬搬无处安放；其效果已由 ③ 覆盖。
- `safety_guards.py` 整套 CommIssueGrace 基建：本地基线太旧，整套搬动侵入面大，简化版即可达成同等安全语义。等后续同步新版 sunnypilot 基线时再对齐。

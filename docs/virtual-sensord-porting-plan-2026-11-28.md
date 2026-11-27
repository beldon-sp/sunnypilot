# virtual sensord 移植方案

**源**: `mouxancnb/openpilot`（CNB，sunnypilot/zoompilot carrot 系，2026-11-14/15 上游基线）
**目标**: 本分支 sunnypilot（BYD 宋PLUS DM-i 2022 分支，基线较旧）
**日期**: 2026-11-28
**状态**: **代码已实施**（2026-11-28，commit `059bed4e9`，byd-tici 未推送）。P3 门禁：PC 单测/重建 ✅，replay ❌ 未做，设备 offroad ❌ 未做（设备中途掉线）。所有"已验证"条目均已在本仓库实际代码核对过。
**2026-11-28 设备实测更新**: P1 门控的 `imu_present()` 探测方式已修正（sysfs 探测在本机不可用，见 §3 P1 第 3 条）；本机 C3L 克隆实测有 LSM6DS3 硬件（WHO_AM_I=0x6a），Stage A 对本设备定位维持"IMU 失效兜底"不变。

---

## 0. 实施落点（2026-11-28，对照 §5 清单）

| 计划项 | 实际落位 | 偏差说明 |
|---|---|---|
| params_keys.h +2 key | ✅ 按 §3 原样（ChassisYawAvailable 在 SP 段 ChevronInfo 前，VirtualSensordEnabled 在 TrueVEgoUI 后，均默认 "0"） | 无 |
| process_config chassis_yaw 谓词 | ✅ 按 brand=='byd' 判定，挂 mapd_manager（与源库同款宿主） | 无 |
| process_config imu_present/互补门控 | ✅ WHO_AM_I 直读探测（fcntl ioctl I2C_SLAVE + reg 0x0F，期望 0x6a），异常时保守返回 True | 按本文件 P1 第 3 条的替代方案落地 |
| chassis_yaw.py + test | ✅ 原样移植；test 的哨兵注释补了 BYD 语义说明 | 无 |
| locationd.py carState 注入 | ✅ 落在 :151 carState 分支；`LocationEstimator.__init__` 签名改为 `(debug, params)`（本地无 params 传入，调用点 :281 同步改） | 计划外小改：构造签名 |
| locationd.cc/.h native | ✅ 常量+成员+handle_car_state 注入+locationd_thread() 读参（:682 UbloxAvailable 之后），与源补丁一致（本地无 imu_calibration 相关，跳过 load_imu_calibration_params 落点） | 无 |
| sensor_synth.py | ✅ can_yaw_ok 改 `CP.brand=='byd'`、删 CHASSIS_YAW_INVALID 常量与哨兵判断、删 hyundai_bit 参数；s2d 改用新移植的 `orientation.sensor_to_device_frame()`（顺带完成差异表 #1 的可选 helper 移植） | helper 依赖 |
| virtual_sensord.py | ✅ 删 ImuCalibrationEnabled 退出检查（含 params/cloudlog 冗余 import）；其余原样 | 无 |
| 契约测试 | ✅ 适配：+`test_no_imu_calibration_exit_check`（ast 摘 docstring 后查代码）、`test_byd_brand_gate_and_sanity`（哨兵 100.0 由 sanity 门拒绝、非 BYD 品牌一律 False） | 测试计数 9→10（math 8 + contract 类） |
| scons 重建 libparams_c | ✅ 本机已重建 `libparams_c.dylib`；PC 上 get_bool 两新 key 正常返回默认 False | 设备侧 aarch64 .so 需随部署链重建 |

**验证状态（P3）**：
- PC：两套纯数学单测 **19 passed**；`locationd.py` 真实 pose_kf 下 carState 开/关两路 handle_log 均 SUCCESS（rednose cython 已本地构建）；manager 门控 PC 侧 real=True/virtual=False 互补正确。
- Replay（Stage B 开关对比）：**未做**。
- 设备 offroad：**未做**——SSH 验证时设备中途掉线（192.168.3.146 不通）；已确认设备侧旧库对新 key 报 UnknownKeyName（符合预期，部署含重建 libparams 后消失）。

---

## 0. 结论摘要

- 源实现分两层，**可以分别移植**：
  - **Stage A — virtual sensord**（合成 IMU 进程）：只在无物理 IMU 的设备上有意义。本机 C3L 克隆有 LSM6DS3（`sensord.py` 走 I2C bus1 + GPIO84 中断），所以对本设备是"IMU 挂了才用"的兜底；对其他无 IMU 的克隆机是刚需。
  - **Stage B — locationd chassis yaw KF 观测**：用 CAN 横摆角速度作为 locationd 的 yaw-only 观测。**对本分支直接有价值**：BYD 的 0x222 YAW_RATE 已在 carstate 解析并实车验证（corr 0.90-0.97），拿去喂 KF 可以改善 locationd 航向估计。
- 好消息：两边 locationd 的传感器帧约定**完全一致**（`[-v2,-v1,-v0]` 重映射），`sensor_synth.py` 纯数学可近乎原样移植；主要改造点是 **capability 门**（源库用 HyundaiExtFlags，本分支无 extFlags 机制，BYD 直接按 brand 判定）和 locationd 基线差异。
- 建议顺序：**P0 参数 → P2 chassis yaw 观测（对本车价值最大）→ P1 virtual sensord（兜底，可后置）→ P3 验证**。

---

## 1. 源实现盘点

### 相关提交
| commit | 内容 |
|---|---|
| `1372affa1` | virtual_sensord.py + sensor_synth.py + 契约测试 + process_config 门控（Stage A，默认关） |
| `7ba0b82f9` | chassis yaw KF 观测（Stage B）：locationd.py + locationd.cc + chassis_yaw.py + ChassisYawAvailable 参数 |
| `e896d6f87` | merge：VirtualSensordEnabled / ChassisYawAvailable 双参数，real/virtual 门控严格互补 |

### 文件清单（源库）
```
openpilot/system/sensord/virtual_sensord.py       # 合成 IMU 进程，104Hz
openpilot/system/sensord/sensor_synth.py          # 纯数学（capnp-free，PC 可测）
openpilot/system/sensord/tests/test_virtual_sensord_contract.py
openpilot/selfdrive/locationd/chassis_yaw.py      # 观测用坐标变换（capnp-free）
openpilot/selfdrive/locationd/tests/test_chassis_yaw.py
openpilot/selfdrive/locationd/locationd.py        # +16 行：carState 分支注入观测
openpilot/sunnypilot/selfdrive/locationd/locationd.cc/.h  # +20 行：native 镜像
openpilot/system/manager/process_config.py        # imu_present() + 互补门控
openpilot/common/params_keys.h                    # 两个新 key
```

### 核心设计（8 条安全契约，移植时必须保住）
1. **默认关**：`VirtualSensordEnabled=0`，且 manager 门要求"无物理 IMU"才启动；real/virtual 严格互补，同一时刻恰好一对 publisher。
2. **source=velodyne**：locationd 只拒 bmx055（接受 velodyne），而 impact/dashcam 白名单不含 velodyne → 合成数据永远不会触发碰撞检测。
3. **timestamp=logMonoTime**（单调时钟）：壁钟时间会被 `_validate_sensor_time`（100ms 容差）拒掉 → inputsOK=false → 无法 engate。
4. **union 显式 init**：`acceleration` / `gyroUncalibrated`，与真 sensord 完全相同的 capnp 契约。
5. **aEgo 在 sensor z 轴取反**：经 `[-v2,-v1,-v0]` 映射后落在 device x = +aEgo，正好是 ExcessiveActuationCheck 读的轴。
6. **重力固定 9.81 放 sensor x**：给 EKF 提供 roll/pitch 绝对参考（camodo 给不了）。
7. **gyro 预旋转 `s2d_inv(D @ gyro_calib)`**：CAN yaw 只覆盖 calib 帧 yaw 轴，保留 camodo roll/pitch（D4 审查结论：清零会喂紧方差假零污染姿态）；预乘 D 后经 s2d 正好落到与 camodo rot_device 相同的约定，gyro-camodo 交叉校验才能过。
8. **CHASSIS_YAW_INVALID 哨兵过滤**（Hyundai 用 100.0）+ ROTATION_SANITY_CHECK=10 sanity 门。

### Stage B 设计要点
- capability 由 manager 每拍从 `CP.extFlags` 镜像到 `ChassisYawAvailable` 参数（locationd 不订 carParams，只能走参数）。
- 观测 = yaw-only PHONE_GYRO：vehicle yaw-up → calib 帧 `[0,0,-yaw]` → `device_from_calib` → device 帧；per-call R = diag(1e6, 1e6, 0.05²) 屏蔽 roll/pitch，不引入新 ObservationKind、不改 pose_kf 方程。
- native 与 python 双实现镜像；关掉时逐字节等价。

---

## 2. 本地分支差异核对表（逐项已验证）

| # | 项目 | 源库（mouxancnb） | 本分支 | 移植影响 |
|---|---|---|---|---|
| 1 | 传感器帧重映射 | `sensor_to_device_frame(v) = [-v2,-v1,-v0]`（orientation.py:54 抽成公共函数） | locationd.py:112/132 内联同样的 `[-v2,-v1,-v0]` | **约定一致**，build_accel/build_gyro 数学直接可用；可选：顺手把这个 helper 移植进 orientation.py |
| 2 | capability 标志 | `CP.extFlags`（conversun opendbc + HyundaiExtFlags.CHASSIS_YAW_RATE=2^20） | **无任何 extFlags 机制**（cereal capnp、opendbc 均无） | BYD 不需要加 cereal 字段：YAW_RATE 0x222 由 carstate 无条件解析，capability 直接按 `CP.brand == 'byd'` 判定 |
| 3 | yawRate 数据源 | Hyundai ESC，带 100.0 哨兵 | BYD 0x222（50Hz 实测），全零 payload 垃圾帧守卫（非就绪时=0.0） | 哨兵检查换成 BYD 语义；yaw=0 时 a_lat/gyro yaw 归零，由 camodo 交叉校验兜底 |
| 4 | yawRate 质量 | 未实车验证（TODO device-verify） | **实车验证过**：corr 0.90-0.97、slope~1.0（docs/byd-carrot2-reference-2026-10-21.md s7.6） | 本分支条件更好 |
| 5 | aEgo | CAN 轮速 KF | 同（carstate.py:149 `update_speed_kf`） | 无差异 |
| 6 | gyro-camodo 交叉校验 | locationd.py 同位置 | locationd.py:135-140 存在（30×σ 门，INPUT_INVALID 计数） | 合成 yaw 必须跟得上 camodo 分布，**必须 replay 验证** |
| 7 | ImuCalibrationEnabled / imu_calibrationd | 有（ImuCalibrationMatrix 机制） | **无**（legacy calibrationd，rpyCalib 只喂 device_from_calib 用于 camodo） | virtual_sensord 的退出检查删掉；build_gyro 的 D 预乘保留（本分支 device_from_calib 同样作用于 camodo rot，保留才能对齐交叉校验约定） |
| 8 | source 校验 | 只拒 bmx055 | locationd.py:72-74 **同样只拒 bmx055**；且本地真 sensord 的 i2c_sensor.py 本来就用 velodyne 当 unknown 默认值 | velodyne 契约本地天然成立 |
| 9 | services 频率 | gyro/accel 104Hz | cereal/services.py:24-25 同为 104Hz | Ratekeeper(104) 原样 |
| 10 | manager sensord 入口 | 单进程 + 互补门控 | process_config.py:145 `PythonProcess("sensord", ..., only_onroad, enabled=not PC)` 单入口 | 拆成 real_sensord / virtual_sensord 两个谓词 |
| 11 | params get_bool 未知 key | 抛 UnknownKeyName → 门控 fallback off | common/params.py:113/134 同样抛 | `_virtual_sensord_wanted` 的 try/except 原样移植 |
| 12 | locationd.cc handle_car_state | standstill 分支 + 注入 | 443-450 行 standstill 分支**与源补丁上下文一致**；OBSERVATION_PHONE_GYRO 已在用（:268）；参数读取在 locationd_thread()（:679） | native 补丁基本原样；参数读取位置改到 locationd_thread() / 构造处 |
| 13 | params_keys.h | +2 key | 无 | 新 key → **需 scons 重建 libparams_c**（已知坑） |
| 14 | 本地已知旧基线差异 | locationd.py 有 timing grace / invalid 计数重构 | 本地无 `update_observation_invalid` / `load_imu_calibration_matrix` 等 | locationd.py 的 +16 行补丁需手工落位（handle carState 分支在 :148），不能 git apply |

---

## 3. 分阶段移植

### P0 — 参数与 capability 镜像（前置，~0.5h）✅ 已实施（059bed4e9）
1. `openpilot/common/params_keys.h`：
   ```c
   {"ChassisYawAvailable", {PERSISTENT, BOOL, "0"}},
   {"VirtualSensordEnabled", {PERSISTENT | BACKUP, BOOL, "0"}},
   ```
2. `process_config.py` 新增谓词（无 extFlags，BYD 直接按 brand）：
   ```python
   def chassis_yaw(started, params, CP):
     available = CP.brand == 'byd'   # 0x222 YAW_RATE 由 byd carstate 无条件解析
     if available != params.get_bool("ChassisYawAvailable"):
       params.put_bool("ChassisYawAvailable", available, block=True)
     return True
   ```
   挂进 procs 里任意常驻进程的 gating（源库做法是挂在某个进程谓词上每拍执行）。
3. scons 重建 libparams_c（已知坑：新 key 不重建会 UnknownKeyName）。

### P2 — locationd chassis yaw KF 观测（对本车价值最大，~1h 代码 + replay 验证）✅ 代码已实施，replay 待做
1. 新增 `openpilot/selfdrive/locationd/chassis_yaw.py`：**原样移植**（capnp-free，30 行，两个纯函数 + 5 个单测）。
2. `locationd.py`（本地 :148 carState 分支）：
   ```python
   from openpilot.selfdrive.locationd.chassis_yaw import chassis_yaw_to_device, chassis_yaw_R
   # __init__: self.chassis_yaw_available = params.get_bool("ChassisYawAvailable")
   elif which == "carState":
     self.car_speed = abs(msg.vEgo)
     if self.chassis_yaw_available:
       yaw_rate_device = chassis_yaw_to_device(msg.yawRate, self.device_from_calib)
       if np.linalg.norm(yaw_rate_device) < ROTATION_SANITY_CHECK:
         self.kf.predict_and_observe(t, ObservationKind.PHONE_GYRO, yaw_rate_device,
                                     np.array([chassis_yaw_R()]))
   ```
   注意：本地 handle_log 的 carState 分支没有 `t` 处理差异需核对（源库直接用 `t`）；BYD 垃圾帧守卫已在 carstate 端完成（非就绪=0.0），注入端不用再查哨兵。
3. `locationd.cc/.h`：源补丁基本原样——成员 `bool chassis_yaw_available = false;`；常量 `CHASSIS_YAW_OBS_MASK_R=1e6`、`CHASSIS_YAW_OBS_STD=0.05`；handle_car_state 注入块；参数读取放本地 `locationd_thread()`（:679 处，与 UbloxAvailable 同风格）。
4. 单测：chassis_yaw.py 的 5 个 PC 单测直接拷贝。
5. **replay 验证**：拿实车 rlog 重放，对比开关前后 liveLocationKalman 的 angularVelocityDevice/orientationNED 稳定性，确认无 finite_check 重置风暴。

### P1 — virtual sensord 进程（兜底，~1h 代码 + 门控验证）✅ 代码已实施（WHO_AM_I 探测落地，见 §0）
1. 新增 `openpilot/system/sensord/sensor_synth.py`：**原样移植**（纯数学无依赖），仅改：
   - `can_yaw_ok`：删 HyundaiExtFlags 依赖 → `if CP.brand == 'byd'`；删 CHASSIS_YAW_INVALID 哨兵（BYD 无此语义，垃圾帧已在 carstate 归零）。
2. 新增 `openpilot/system/sensord/virtual_sensord.py`：原样移植，仅删 `ImuCalibrationEnabled` 退出检查（本地无此机制）。
3. `process_config.py`：
   - `imu_present()`：**不能用源库的 sysfs 探测**。源库探测 `/sys/bus/i2c/devices/1-006a`，但 2026-11-28 设备实测：本机（C3L 克隆，AGNOS 20.6.102，内核 4.9.103）**bus1 无 sysfs 设备节点，而 IMU 硬件实际在线**（`/dev/i2c-1` 直读 0x6a `WHO_AM_I=0x6a` 正常应答；用户态 sensord 是 comma 风格 i2c-dev 直读，不依赖内核枚举）。sysfs 探测在本机必然返回 False → 若 `VirtualSensordEnabled=1` 会把**本来健康的真实 sensord 关掉换成合成数据**——误判方向是危险的一侧。
   - 替代方案（实测可行）：WHO_AM_I 直读探测（`/dev/i2c-1`，I2C_SLAVE=0x0703，读 reg 0x0F，期望 0x6a；源库 docstring 自己也指出"WHO_AM_I probe is stronger but unverified"，现已在设备上验证）；或最保守——`imu_present()` 恒 True（virtual sensord 本来就只该在人工确认 IMU 失效的设备上开）。
   - 拆 :145 的 sensord 入口：
     ```python
     PythonProcess("sensord", ..., real_sensord, enabled=not PC),
     PythonProcess("virtualsensord", "openpilot.system.sensord.virtual_sensord", virtual_sensord, enabled=not PC),
     ```
     `real_sensord` / `virtual_sensord` 严格互补（源库实现原样）。
4. 契约测试 `test_virtual_sensord_contract.py`：原样拷贝（本地 params/process_config 行号引用需微调）。

### P3 — 测试与验证门禁（不跳步）⏳ 第 1 步已过，2-5 未做
1. **PC**：两个纯数学单测套件全绿（无 msgq 依赖）。
2. **Replay**：
   - Stage B：开关对比，KF 无发散、无 reset 风暴、gyro-camodo 交叉校验无 INPUT_INVALID 计数上涨（重点看急弯 + 模型输出滞后的时刻）。
   - Stage A（如做）：rlog 中插入合成流（或直接在 replay 环境跑 virtual_sensord），确认 `_validate_sensor_time` / `sensor_all_checks` 全通过。
3. **Device offroad**：确认 sysfs 节点存在性、`ChassisYawAvailable` 被正确置 1、`VirtualSensordEnabled` 默认 0。
4. **Onroad（Stage B，默认关先验证"关=无影响"）** → 参数打开 → 短途实车 → 看_ublox/位置精度与 alerts。
5. Stage A onroad 前置条件：仅在 IMU 确认失效的设备上开；a_lat 符号与 sensor→device 轴向按源库 TODO 用左右弯路线实车确认（源库自己也没过这道，见其 `TODO(device-verify)`）。

---

## 4. 风险清单

| 风险 | 等级 | 缓解 |
|---|---|---|
| 合成 yaw 与 camodo 分布偏离（模型滞后/急弯）→ 交叉校验 INPUT_INVALID → inputsOK false | 高（Stage A） | replay 专项验证；必要时给 virtual gyro 加与 camodo 的软钳位再上车 |
| BYD 全零垃圾帧=0.0 与"真直行=0"不可区分 → 假 yaw 观测 | 中 | 0.0 是合法观测值且与 camodo 直行一致，无实际危害；但 Stage B 注入端可加 `abs(yawRate)>1e-6 or standstill` 的冗余判断（可选） |
| a_lat / 轴向符号错（源库未实车验证的部分） | 高（Stage A onroad） | 左右弯实车路线验证后才允许 onroad |
| params 新 key 未重建 libparams_c → manager 谓词崩溃 | 中 | `_virtual_sensord_wanted` try/except fallback off（源库契约测试锁定此行为） |
| 本地 locationd 基线较旧，+16 行补丁手工落位引入笔误 | 中 | 落位后 diff 自查 + replay 开关对比"关=逐字节等价" |
| scons 重建遗漏 | 低 | 已知坑，P0 完成即重建验证 |

## 5. 改动文件清单（目标仓库）

```
M  openpilot/common/params_keys.h                        (+2 key)
M  openpilot/system/manager/process_config.py            (chassis_yaw/imu_present/互补门控)
M  openpilot/selfdrive/locationd/locationd.py            (+~14 行，carState 分支)
M  openpilot/sunnypilot/selfdrive/locationd/locationd.cc (+~17 行)
M  openpilot/sunnypilot/selfdrive/locationd/locationd.h  (+1 成员)
A  openpilot/selfdrive/locationd/chassis_yaw.py          (原样)
A  openpilot/selfdrive/locationd/tests/test_chassis_yaw.py (原样)
A  openpilot/system/sensord/virtual_sensord.py           (小改)
A  openpilot/system/sensord/sensor_synth.py              (can_yaw_ok 改 BYD 门)
A  openpilot/system/sensord/tests/test_virtual_sensord_contract.py (原样)
   + scons 重建 libparams_c
```

## 6. No-Go 纪律（不变）

- 推送开发分支 ≠ 放行；P3 各门禁未过不上实车。
- Stage B 上实车的前提：replay 对比无回归 + 默认关验证通过。
- Stage A 上实车的前提：目标设备 IMU 确认失效 + 轴向/符号实车验证通过（源库自身都未过此门）。

# mouxancnb/openpilot 第二轮挖掘（2026-11-28）

> 第一轮已覆盖：AGNOS 全套、virtual sensord、CJK 字体管线、locationd 鲁棒性、tc-v61 probe、webui。
> 本轮扫全量提交列表（~120 条近三个月），覆盖剩余未挖的领域。所有本地判定均实际查过代码。

## 摘要表

| 提交 | 内容 | 本地适用性 | 判定 |
|---|---|---|---|
| `eeec3d329` | radard capnp 泄漏 gc.collect | 本地同款风险存在 | ✅ **P0，已移植 `5d12cc610`** |
| `9638bc826` | modem 重拨指数退避 | 本地同款 bug 存在 | ✅ P1，已移植 `c37f6eae5` |
| `3beceba98` | 盲区音频提醒（StarPilot port） | BYD BSM + 全部依赖齐备 | ✅ P1，已移植 `db2440b90`（10 测试通过） |
| `dee2b932c` | updated 清理设备本地脏树 | 设备有手工热修习惯 | ⚠️ P2，按需 |
| `71e5ee099` | 曲率学习限速（等张回归） | 本地无 VisionCurveSpeed 地基 | 🔮 方向，架构借鉴 |
| `b1f06362c`/`5b3256c29` | 红绿灯停车起步 / CEM 场景触发 | 依赖 carrot 纵向状态机+CEM，本地均无 | 🔮 方向 |
| `b17180b07` 等 | 息屏真下电（亮度0+bl_power=4） | 克隆屏相关，路径需自行映射 | ⚠️ 语义参考 |
| `6bd881338`/`5e187039d` | TTL 缓存 / 空闲 CPU 优化 | 本地 carrot_man 仅 5 读/tick | ⛔ 收益小 |
| `33376820b` | camerad 恢复耗时观测 | 纯观测 | ⚠️ 可选 |

---

## 1. radard capnp 泄漏 gc.collect（`eeec3d329`）— P0 ✅ 已移植（`5d12cc610`）

**落地记录（2026-11-28）**：`common/realtime.py` 新增 `PeriodicGC` 助手（帧计数 + 周期 `gc.collect()`，注释含事故链数据），审计后按文档建议三处全加：
- `selfdrive/controls/radard.py`：600 帧 @20Hz ≈ 30s；
- `selfdrive/car/card.py`：3000 帧 @100Hz ≈ 30s（逐帧 churn carState/carOutput/radarTracks/carStateSP）；
- `selfdrive/locationd/locationd.py`：600 帧 @20Hz ≈ 30s（deviceMotion，内存压力即 locationdTemporaryError 诱因之一）。

**源库事故链（实测数据）**：`config_realtime_process` 关闭循环 GC（上游行为）→ radard_dpath 逐帧 churn capnp builder、wrapper 形成引用环 → 设备实测 **1.1 GB/h 泄漏**（177→472MB/22min）→ 长途撑爆 3.5GB → lowMemory 告警 + 页回收卡顿 → **locationdTemporaryError**。修复：每 600 帧（20Hz 下 ~30s）`gc.collect()` 一次，保留量压到 ~20MB，暂停几 ms 对 50ms 预算无感。

**本地核对**：
- `common/realtime.py:42` **同款 `gc.disable()`**；
- `selfdrive/controls/radard.py:302` 同样 `config_realtime_process(5, ...)` 且逐帧发布 liveTracks capnp。
- **本地同款泄漏风险真实存在**，且这条事故链和 locationd 鲁棒性修复目标是同一条（内存压力诱发的中途误脱开）——你已经在修 locationd 的"果"，这个修的是"因"之一。

**落地**：~10 行（帧计数 + 周期 collect + 注释）。建议顺手审计所有 `config_realtime_process` 进程（radard、card、locationd）是否都有逐帧 builder churn，全加。

## 2. modem 数据会话重拨指数退避（`9638bc826`）— P1 ✅ 已移植（`c37f6eae5`）

**落地记录（2026-11-28）**：源补丁四处改动原样移植到 `common/hardware/comma/modem.py`——`import random`、`CONNECT_BACKOFF = (1,2,4,8,15)` 梯子常量、`_bump/_reset/_wait_backoff` 三方法（`_wait` 分 0.5s 步进可被 stop() 打断）、`_handle_pppd_exit` 先等当前档再升档 + 失败日志合并、`_poll` 见 `connected=True` 复位。

**源库问题**：抖动 modem 每 ~1s 无限重拨，实车表现为重连风暴占满一个核。修复：1s→2s→4s→8s→15s 上限指数阶梯 + jitter，会话真正恢复后复位；PPP/QMI 共用路径 `_handle_pppd_exit` 一处生效；退避 sleep 可被 stop() 打断。

**本地核对**：`common/hardware/comma/modem.py:409` `_handle_pppd_exit` **无退避、立即重拨**，同款 bug 存在。~25 行可原样移植（本地该函数结构与源库补丁前一致）。

## 3. 盲区音频提醒（`3beceba98`，StarPilot port）— P1 ✅ 已移植（`db2440b90`，10 测试通过）

**落地记录（2026-11-28）**：
- `sunnypilot/selfdrive/selfdrived/blindspot_alert.py`：纯函数门控模块原样移植（告警文案"盲区检测到车辆"、warningSoft、防御门齐全）；
- `selfdrived.py` 五处注入：import、__init__ 默认三元组 + `car_state_log_ns`、data_sample 记 carState logMonoTime、update_alerts 注入（替换 `laneChangeBlocked/warning`、不进 events map）、params_thread 周期读参；
- `common/params_keys.h` 新增 3 键（PERSISTENT BOOL）；
- `tests/test_blindspot_alert.py` 适配本地（删 mouxancnb 特有的 `_DEFAULT_NAV_PARAMS`/`PARAM_LABELS`/`e2eChime` 检查，stub 改 try/except 兜底，AlertEntry 改模块级导入），10/10 通过。

**待办**：params_keys.h 新 key 需 scons 重建 libparams_c 后上设备；UI 面板开关（sp_param_labels 对应物）本地暂无，先用 param 命令行/接口设置。

**功能**：把 OEM BSD 信号变成两类声音提示（均为 opt-in，默认全关）：
- **LoudBlindspotAlert**：变道被 BSM 阻断时，用更响的提示替换 laneChangeBlocked 提醒；
- **LoudBlindspotAlertWhenDisengaged**：横向未接管时，打转向灯指向有车盲区 → 播提示音；
- 附 MuteAtStandstill 选配。

**工程质量**：纯函数模块（`blindspot_alert.py`），门控防御性极强——carState>200ms 旧、deviceState>2s 旧、CAN 无效/超时一律返回 None 静音；无 BSD 车型字节级零变化。不进 events map（SP 事件枚举冻结），直接注入 AlertManager，HIGHEST 级接管告警仍压过它。

**本地核对（全部齐备）**：
- `CS.leftBlindspot/rightBlindspot` 已由 BYD `BSD_RADAR` 解析（UI 三处已在消费）；
- `selfdrived.py:415` 已有 `EventName.laneChangeBlocked`；
- `sunnypilot/selfdrive/selfdrived/events_base.py:43` 有 `class Alert`，导入路径兼容；
- 本地 desire_helper "BSM always blocks" 已有阻断逻辑 → 音频层是纯增益。

**落地**：模块 ~80 行（防御门可精简）+ selfdrived 注入 ~30 行 + params_keys.h 3 个 key（记得 scons 重建 libparams_c）。

## 4. updated 清理设备本地脏树（`dee2b932c`）— P2

scp 热修、手工实验在主仓/子模块留下的 tracked changes 和 untracked 文件，会让 fetch 循环里 `git submodule update` 拒绝 checkout、整个更新失败。修复：check/fetch 前自动清理设备本地改动（含子模块）。

**本地相关性**：你的设备 `/data/openpilot` 是 release action 产物 + launch_env 手工维护 + overlay 类实验，正是这个 bug 的温床。如果将来 OTA fetch 出现莫名失败，先查这个。~50 行，按需移植。

## 5. 曲率学习限速（`71e5ee099`）— 🔮 方向，架构借鉴

24 桶 log-curvature **等张回归**（isotonic）舒适层 + 历史权重上限 600 + v1 JSON 持久化 + nudge 通道，叠在 VisionCurveSpeed 几何层之上当每点横向加速度预算。默认关、comfort_fn 需文档有效且至少学到一个样本才激活。

**本地判定**：不能直接搬——依赖本地没有的 VisionCurveSpeed 地基（本地只有 carrot `curve_speed.py` SLC 源）。但"**学习层叠在几何层上、学到的才生效、默认行为零变化**"的架构，正是你 `route_curvature.py` 曲率预减速的未来演进方向：几何预算先兜底，实车跑出来的舒适上限逐步覆盖。

## 6. 红绿灯停车起步 + CEM 场景触发（`b1f06362c`/`5b3256c29`）— 🔮 方向

红绿灯三层根因修复（CarrotLongitudinalSourceEnabled/CarrotEnabled 默认 0 导致全链死亡——**默认值改动的全链排查方法论值得学**）+ CEM 红灯检测器（0.8s/0.6s 滤波、**veto-only release：plan/model 的 stop 位永远不能 ACQUIRE 只能 VETO**）+ 信号灯车道限定 + 低速 chill 场景。依赖 carrot 纵向 6 状态机和 CEM conditional mode，本地均无。**veto-only release 的安全设计原则**在将来任何停车/起步功能里都值得照抄。

## 7. 息屏真下电（`5593f36c8`→`31bfea77a`→`b17180b07`）— ⚠️ 语义参考

演进三步：克隆屏 TSP 保活参数选配 → 唤醒面板下电 bl_power=4 → 息屏亮度 0 + bl_power=4 完全对齐 carrot 方案、保活撤线。关键结论：**DCS 亮度 0 时面板仍留微弱发光，bl_power=4 下电才真全黑**。你设备的克隆屏此前也有触摸/保活问题，最终语义（真下电 + 保活彻底撤线）可参考；你的 UI 是 raylib DRM 直连，下电路径需自行映射到你的面板。

## 8. 明确不搬

- **TTL 缓存 UnifiedParams / 1Hz 循环降 CPU**（`6bd881338`/`5e187039d`）：本地 carrot_man 每 tick 仅 5 次 param 读，无同类热点，收益不成比例。
- **camerad 恢复耗时观测**（`33376820b`）：纯观测，可选，排查 camerad 恢复问题时再加。
- **CEM/红绿灯/HEV navd/mapsd 栈**：地基缺失，见 §6。
- **git_installer 多主机探针**（gitclone.com 代理、安装前校时）：你走 release action 推 CNB，不依赖 git 安装路径。

## 建议动手顺序（结合第一轮结论）

1. ~~radard gc.collect（§1）~~ ✅ 已完成（`5d12cc610`，radard/card/locationd 三处）；
2. ~~modem 退避（§2）~~ ✅ 已完成（`c37f6eae5`）；
3. locationd ④+①（第一轮已定）——与 §1 同属"消灭中途误脱开"主题；
4. ~~blindspot_alert（§3）~~ ✅ 已完成（`db2440b90`，默认关可安全上车，待 scons 重建 + 设备验证）；
5. navi generation re-arm、AGNOS 一致性门、字体两个小修（第一轮已列）。

> 上车提醒：`LoudBlindspotAlert*` 三个 key 与本批代码均未推送 origin/byd-tici；params_keys.h 改动需 scons 重建 libparams_c.dylib 才能在设备生效。

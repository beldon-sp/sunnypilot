# carrot navi tc-v61 probe：源实现 vs 本分支现状

**日期**: 2026-11-28
**源**: mouxancnb/openpilot `e38fef53a`（carrot navi tc-v61 probe P1/P2/P3 device-side extensions，454 行新增，66 个离线单测）
**结论先行**: 这是**比我本地栈新一代**的地基上的扩展。本分支有自己的 carrot navi 线（`selfdrive/carrot/`：carrot_navi_cereal + navi_alerts + navi_lane_change_gate），P2 预警的"一次性 + 重武装"设计两边已独立收敛；probe 提交里**真正值得搬的是 P3 的 generation 翻转撤销模式 + 两个新 advisory + 一组防御性解析纪律**。P1（HD 车道级数据路径）依赖本地没有的 LaneHD/fusion/coordinator 地基，属未来方向。

**实施状态（2026-11-28 晚，§3 的 ①②③ 已全部落地，commit 见 git log byd-tici）**：
- ① re-arm 模式：已实现，但**触发信号做了纠正**——本地 `CarrotNaviState.generation` 实测是接收端"任意状态更新"计数器（`carrot_navi.py::_mark_state_changed_locked` 每次 App 上行都 +1），不是源库那种 OnMainRouteChanged/RouteDeleted 路线代，直接拿它当翻转信号会每帧 re-arm、一次性语义名存实亡。改用路线状态三元组 epoch（`route.meta.present` / `navigationStatus.routePresent` / `offRoute` 任一边沿翻转 ≈ 重算路/路线消失恢复），见 `navi_alerts.NaviAlertTracker._rearm_if_route_changed`。
- ② 两个 advisory：`carrotNavConsecutiveTurns` @29 / `carrotNavArrivalSoon` @30 已进 `custom.capnp`，tracker 一次性语义 + 优先级链插入（偏航 > 红灯 > 连续转弯 > 即将到达 > 转向预告——一次性预警必须排在连续预警之前，否则会被永久压住），EVENTS_SP 回调 + 测试齐。`navLaneDowngrade` 维持"搬不了"（无 HD 数据源）。
- ③ 解析纪律：`carrot_navi_cereal._finite` 加 JSON bool 排除（`_integer` 经由它覆盖）；"每字段独立 Valid + fail-inert"原则已体现在新 advisory（`is_turn_family` 映射查不到一律不发）。
- gate 侧：`NaviLaneChangeGate` 核实**无跨帧抑制缓存**（block_reason 每帧现算），翻转无需清理动作；模块头注明不变量，`TestFrameIndependence` 用测试钉死。
- 测试：111 passed（test_navi_alerts 52 / test_navi_lane_change_gate 24 / test_carrot_navi 30 / test_carrot_serv_decision 5 等，`.venv/bin/python` + PYTHONPATH）。
- 未推送/未刷设备：纯预警层，不触变道控制，`NaviGuidedLaneChangeEnabled` 门不变，No-Go 门禁不变。

---

## 1. 源实现盘点（tc-v61 = 腾讯地图探针 App 的 HDLaneMatchResult 车道级数据）

设计总纲：**设备端被动接收，每个新字段可选**——App 不发时全部降级 inert，pre-probe App 行为逐比特不变。

### P1 数据路径（LaneHD 扩展）
- custom.capnp `CarrotNaviStateSP.LaneHD` 增 4 个探针字段，**每个带独立 Valid 标志**：`laneRatio`（匹配置信比 0..1）、`matchedIndex`（匹配车道点）、`logoDisplayType`、`locLaneTypes[]`（逐车道类型码）。
- `_lane_hd` 解析防御性拉满：JSON bool 永远不当作数字通过校验、数值带 min/max 钳位、列表限 Int16。
- fusion 层加**二级质量门**：两个置信度（总车道数 conf、当前车道 conf）之外，`lane_ratio < 0.5` 时把 HD 状态降级为普通可用性路径（advisory-only，`ratioGated` 标注）；ratio 缺失则完全跳过此门（= pre-P1 行为）。

### P2 一次性预警（全部挂在 `CarrotNavLaneCoordination` 参数门后，与 navExitAhead 同级）
| 预警 | 触发 | 语义 |
|---|---|---|
| `navLaneDowngrade` | 探针 workMode 从 HD 翻到 SD | 静默恢复，每个 HD 时段只响一次 |
| `navConsecutiveTurns` | 当前 + 下一 maneuver 都是 turn/rotary 且下一个在 400m 内 | 连续转弯提前准备；**未知 turnCode 永远 fail-inert**（turn-family 表查不到 → 不发，绝不误发） |
| `navArrivalSoon` | 引导中剩余里程首次进入 500m | 纯提醒，不自动减速 |

`carrotNaviSP.generation` 翻转（探针 OnMainRouteChanged/RouteDeleted，即重算路）→ **所有 advisory 重新武装**。

### P3 协调层：generation 翻转撤销已武装方向
`CarrotNaviLaneCoordinator`：route 重算时，翻转瞬间已武装的方向约束被撤销，直到 atcType 变化（新 maneuver）或消失。注释点明安全构造：**撤销只可能移除约束，永不添加**——fail-safe by construction。P4（探针数据直接发起变道）作者自己也没做，留给专门安全评审。

---

## 2. 本分支现状（逐项已验证）

本地的 carrot navi 线是**同源但早一代**的独立实现，布局不同：

```
custom.capnp: CarrotNaviState @0xc2243c65...（自有槽位，port from carrotpilot）
  已有：generation @1、guidanceCurrent/Next（turnType/distanceM/meta.present）、
        Lane（7714 available 数组风格，无 HD 字段）、Speed、trafficSignal、crossroad、route、navigationStatus
  服务 "carrotNavi" 2Hz
selfdrive/carrot/:
  carrot_navi_cereal.py   _finite/_integer/_int16_list/_guidance/_lane 防御解析（与源库同族 helper）
  navi_alerts.py          NaviAlertTracker：carrotNavOffRoute / carrotTrafficSignal / carrotNavTurn，
                          一次性语义（与源库 P2 设计独立收敛，思路一致）
  navi_lane_change_gate.py NaviLaneChangeGate：路口抑制 + 方向冲突（nav_turn_types.turn_direction）
                          + NaviGuidedLaneChangeEnabled 门 + 掉线 1.5s 回退
  nav_turn_types.py       自建转向分类（对应源库 NavTypeMapper.resolve 的角色）
```

| 能力 | 源库（mouxancnb） | 本地 |
|---|---|---|
| App 数据源 | tc-v61 探针 App（7706 navLaneGuide / 7714 available / HDLaneMatchResult lane_ratio 等） | 自有 carrotmate 集成（7714 风格 available 数组，无 HD 探针字段） |
| 车道级 HD 匹配 | LaneHD + fusion（双置信度 + ratio 二级门 + 2s 新鲜度） | **无**（gate 用路口/转向冲突启发式，不到车道级） |
| 变道约束层 | CarrotNaviLaneCoordinator（atcType → 约束 + 新鲜度门 + generation 撤销） | 自己的 NaviLaneChangeGate（抑制式而非武装式，无 generation 处理） |
| 一次性预警 | navExitAhead + P2 三件套 | carrotNavTurn / OffRoute / TrafficSignal（**等价设计已存在**） |

---

## 3. 价值判断与建议吸收

### ① P3 generation 翻转模式（最值得搬，~15 行）
本地 `CarrotNaviState.generation` 字段**已经在 capnp 里**，但本地两层都没消费它：
- **navi_alerts.py**：`NaviAlertTracker` 的一次性状态（carrotNavTurn 已响过就不再响）在重算路后不会重武装——改了路线后新的转向预告会被吞。补：`generation` 翻转 → 清空 tracker 一次性状态（对齐源库 re-arm 全部 advisory 的做法）。
- **navi_lane_change_gate.py**：若 gate 内任何抑制/记忆状态跨帧保持，重算路后旧路线的抑制依据已失效。补：翻转时清 `_block_reason` 依赖的缓存状态。原则照抄源库注释——**翻转只允许移除抑制，永不添加**。

### ② 两个新 advisory（可加入 navi_alerts.py，各 ~20 行）
- `navConsecutiveTurns`：本地 substrate 全齐（guidanceCurrent/Next.present + turnType + distanceM），分类用本地 `nav_turn_types.turn_direction` 判 turn/rotary 家族即可，不必搬 NavTypeMapper；保持"分类失败 → 不发"。
- `navArrivalSoon`：`route.present + remainingDistanceM ≤ 500 + navigationStatus.guidanceActive`，一次性。本地 Route/NavigationStatus 结构已有。
- `navLaneDowngrade`：本地无 HD 数据源，**搬不了**，等 P1 地基。

### ③ 防御性解析纪律（搬原则不搬代码）
- **每字段独立 Valid 标志，False = "缺失"，永不当作 0**——本地将来加任何 App 字段都按这个纪律。
- JSON bool 永不通过数字校验（Python 里 `isinstance(True, int)` 是 True，源库在 `_lane_hd` 里显式排除 bool——本地 `_finite/_integer` 若无此排除，值得补上）。
- 未知枚举码 fail-inert 而非 fail-wrong。

### ④ P1 HD 车道级路径（未来方向，不建议现在动）
依赖三块本地没有的地基：LaneHD capnp 结构、fusion 层（hd_lane_state 双置信度 + ratio 门 + 新鲜度）、以及**App 端探针支持**（tc-v61 App 发 HDLaneMatchResult JSON）。你的 carrotmate App 是否会发这些字段是前置问题。若将来要做，源库 `carrot_navi_fusion.py` 的质量门阶梯（双 conf ≥ 0.5 → ratio ≥ 0.5 → 全过才允许 HD 级阻断）和 2s 新鲜度门是现成蓝本。

---

## 4. 风险与边界
- ①② 都是纯预警/状态清理层，不触变道控制逻辑本身；`NaviGuidedLaneChangeEnabled` 门不变，No-Go 门禁不变。
- ② 的 advisory 事件需在 events.py / UI 侧有展示位（源库在 OnroadEventSP.EventName 加了 @39/@40/@41；本地 custom.capnp 是自有槽位，追加成员同样安全——capnp 成员追加向后兼容）。
- 单测：源库 66 个离线测试的模式可参照（fusion 解析 + coordination 撤销各一组），本地 test_navi_alerts.py / test_navi_lane_change_gate.py 已有骨架。

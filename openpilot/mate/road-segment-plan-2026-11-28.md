# mate v6.3 提案：`road` 段——模型道路几何与多目标采样（2026-11-28，rev2）

> 读者：mate 服务端实现者。提案方：carrotmate（Android 车机端 SR 客户端）。
> 结论先行：**新增一个 `road` 段（frame 推 + snapshot 拉，同源同函数），
> 把 modelV2 的道路几何与时序目标在固定距离栅格上采样后下发。**
> 不推逐点序列、不加新 kind、协议版本保持 6（additive，记 v6.3），
> 老客户端忽略未知段即零影响。
>
> **rev2（2026-11-28 双侧核对后修订）**：对照本仓代码与 carrotmate 客户端逐项核实，
> 推翻 rev1 的三条基础断言并连带修正 §4.4/§4.5/D2/D5：
> ① `laneLines.x` 不是「各时刻纵向位移」，是**恒定空间栅格**（X_IDXS，0→192m，
>    `fill_model_msg.py:107`）——rev1 把它和 `position`/`dmodel.path` 的时间域混了
>    （那两个才是 T_IDXS 位移，客户端 `SRSceneModels.kt:8` 注记可证）；
> ② 本仓 modelV2 XYZTData 的 **y 是右正**（非左正）——服务端三处证据
>    （`ldw.py:31-32`、`radard.py:164` `yRel=-lead.y[0]`、`model_renderer.py` lanes
>    原样 vs lead 取负同投影）+ 客户端早先核实（`SRSceneModels.kt:8-10` 引
>    camera.py:73）。carrotmate 作者与本仓互相印证，rev1「已核实左正」不成立；
>    输出契约仍定左正，但**服务端必须在采样处显式取负**（恰好 = webui 原 negation）；
> ③ 客户端新鲜度口径是**本地收包时刻**（明确禁用设备墙钟 ts，`SRSceneModels.kt:13`），
>    不是 rev1 写的「以 road.ts 判新鲜度」。

## 1. 动机与来源

carrotmate 的 SR（环境可视化）目前是俯视 2D Canvas，数据面只吃 `frame` 的
car/radar 与快照 dmodel.path。计划做透视呈现升级，缺的不是前端代码——是
**道路几何（车道线/路缘）与模型侧多目标**这两类数据。

sr-view spec 现有红线：路带（dmodel.path 求值）必须常显「路径带 · 非车道线」
防语义误导（`openspec/specs/sr-view/spec.md` §伪车道线防护）。本段就是让
「真车道线」第一次成为合法元素——实现时路带标注红线保留，lane 线独立绘制。

参考实现：`mouxancnb/webui` 的 `server/bridge/state_api.py::_road_model()`
（配合前端 `road_lite.js` 透视场景）。它的核心设计事实值得吸收：

- **不推逐点序列，推固定距离栅格采样值**（dists = 5/15/30/50/80/120/160 m，
  每条线 7 个数）。栅格化后 ~0.7KB/帧（6 线×7 数 + probs + ≤3 leads ≈ 70 个数），
  正好回应 mate「逐点序列体积大不进快照」的既有顾虑（API.md §5.3）。
- 采样在服务端做（纯 Python、微秒级），客户端拿到即画，无模型依赖。

已评估并排除的方案见 §6。

## 2. §0 定位审计（先过这条）

| 检查 | 结论 |
|---|---|
| op 能获取、客户端无法自产？ | ✅ modelV2/drivingModelData 只在设备上产生 |
| 客户端侧数据回流？ | ✅ 无——**明确不含** CarrotNaviSP.laneCurrent 等 carrotmate 上行的车道样式（见 §6.2） |
| kind 白名单变化？ | ✅ 无。road 是 frame 的新段 + 纯快照段，与 v4 carrot 段完全同构 |

## 3. 本仓 schema 实况（rev2 已按代码逐项核实）

本仓 `modelV2` 消息类型是 **ModelDataV2**（`openpilot/cereal/log.capnp:1052+`），
webui 评估的 fork 是新版 ModelV2，字段类型不同，但**坐标域语义相同**
（同为 device frame、y 右正、lead 概率标量）——rev1 的「不能直接抄」修正为
「类型解包要改，符号/算法逻辑可照搬」：

| webui 用的 | 本仓实况 | 移植影响 |
|---|---|---|
| `model.laneLines : XYData`（x=距离域） | `laneLines : List(XYZTData)`；**x = `ModelConstants.X_IDXS` 恒定栅格**（33 点，192·(i/32)²，0→192m；`constants.py:10`、`fill_model_msg.py:107`） | 与 webui 同为距离域，`_sample_y` 线性插值**原样可抄**；x 不随车速变化，rev1 的「低速 x 序列短」不存在 |
| `model.roadEdges` | ✅ `roadEdges : List(XYZTData)` + `roadEdgeStds`，x 同为 X_IDXS（`fill_model_msg.py:115`） | 直接可用 |
| `model.laneLineProbs` | ✅ `List(Float32)`，恒 4 值（线始终 init 4 条，`fill_model_msg.py:104`） | **逐线有效性的唯一信号**，见 §4.4 |
| `model.path : XYData(+std)` | ❌ ModelDataV2 无 path；路径在 `drivingModelData.path`（**XYZPolynomial，自变量 t 秒**——这才是时间域位移，客户端 `SRSceneModels.kt:143-164` 已按此求值） | 本提案不发 path——carrotmate 已自持 dmodel.path 并在客户端求值，重复下发反成双通道（§6.1） |
| `model.leadsV3` | ✅ `LeadDataV3`（`log.capnp:1099+`）：`prob` 是 **Float32 标量**（非数组，rev1 `prob[0]` 写法会抛），x/y/v/a/xStd/yStd 是 6 样本列表（`LEAD_T_IDXS=[0,2,4,6,8,10]`，秒） | 过滤用 `l.prob`；x[0]≈当前距离，见 §4.5 |
| `CarrotNaviSP.laneCurrent` → line_kinds | 本仓有，但属回声 | **不发**（§6.2） |
| y 符号 | **device frame y 右正**（证据：`ldw.py:31-32` 左线判据 `y[0] > -(...)`；`radard.py:164` `yRel = -lead.y[0]` 才得到左正 yRel；客户端 `SRSceneModels.kt:8-10` 独立核实并引 camera.py:73） | **采样处统一取负**，输出左正；heading 的 negation 照搬 webui（rev1「与 webui 相反」是基于误读的假差异） |

## 4. 契约设计

### 4.1 接线位置

- `poller.py`：
  - 新增纯函数 `road_sample(m) -> dict | None`；
  - frame 构造处 `{car?, radar?, carrot?}` 新增 `road?` 段——
    `frame_payload` 加参、`FrameTicker` 加 `.road` 槽（`poller.py:766-798`）；
  - 同一函数产出快照段（`set_state("road", ...)`，与 car/radar 的
    「一个拉一个推、同源同函数」惯例一致）；
  - 触发位点：`sm.updated["modelV2"]` 分支（可与既有 model 段重建同处，
    `poller.py:1014`）；poller 头部 docstring 的 frame 段注释同步更新；
  - `test_poller.py:308` 既有 `frame_payload` 断言需带 road 参数更新。
- `server.py`：**零改动**。`Hub.emit` 对 frame 整体 JSON 透传，`set_state` 无
  段名白名单（已核实 `server.py:832+`）；`PARAMS`/kind 白名单不动。

### 4.2 出现与停推规则（对齐既有 frame 语义）

- `modelV2` 从未收到（offroad 启动初期）→ `road` 段不出现；
- 模型帧无效（`modelV2.valid == false` 或 `sm.valid["modelV2"]` 未过——
  预热/遮挡期模型会输出全零「笔直车道」，必须挂此门）→ 整段不发；
- offroad 数据源停止 → 自动停推，last-wins；**客户端以本地收包时刻判新鲜度**
  （SR 现行口径：禁用设备墙钟 ts 防钟差，`SRSceneModels.kt:13`；frame 信封
  已带包级 ts，段内无需自带 ts）；
- 快照段在停发后保持最后一次 onroad 值（与 car 段一致）。

### 4.3 字段表（JSON 形状）

```jsonc
"road": {
  "dists": [5, 15, 30, 50, 80, 120, 160],   // 栅格，米；恒定数组，客户端可硬编码/缓存；改栅格=改版本注记
  "lines": [[...7], [...7], [...7], [...7]], // laneLines 4 线 y@栅格，米，左正（服务端已从右正取负）；prob 低于阈值 → 整线 null
  "edges": [[...7], [...7]],                 // roadEdges 2 缘；同 null 规则（edge 无逐线 prob，用 laneLineStds/客户端自判，一期不 null）
  "probs": [0.9, 0.8, ...],                  // laneLineProbs 全 4 值，两位小数——与本段 lines 同帧一致（区别于 model 快照段的快照域 probs）
  "leads": [                                 // leadsV3 过滤后（§4.5），近在前按 d 显式升序排，≤3 个
    { "d": 31.2, "y": 1.9, "prob": 0.71, "v": 12.4, "a": -0.3,
      "xStd": 1.1, "yStd": 0.4, "heading": 0.02 }
  ]
}
```

- 所有横偏类数值 **左正**（与 radar 段 yRel 同约定；客户端场景同为左正
  `SRSceneCanvas.kt:186`，road 数据可直用，无需像 path 那样在绘制层取反）；
- **与 model 快照段的分工**：road=几何采样域（frame+snapshot 推），
  model=决策摘要域（纯快照）。API.md §5.3 加注，防后人往 model 段塞几何。

### 4.4 采样算法（rev2 重写）

x 是恒定栅格，问题退化为「在 33 点固定 x 网格上把 y 插值到 7 个栅格距离」：

1. 逐线有效性：`laneLineProbs[i] < 0.5`（默认值，见 D5'）→ 该线 `lines[i] = null`。
   ——rev1 的 `n≥2 且 x[-1]>x[0]` 判据恒真（x 固定 33 点 0→192m），删除；
2. 每个栅格距离 d：在 X_IDXS 上找区间线性插值 y，两位小数。
   栅格最大 160 < x[-1]=192，**不存在尾部越界**——rev1 的「钳位 vs null」
   （D2）是伪决策，两选项恒等价，直接插值即可；
3. 整帧有效性：`modelV2.valid == false` → 返回 None（§4.2）；
4. ~~静止/蠕行退化~~——rev1 的 `x[-1] < 20` 永不成立，删除。**蠕行/静止时
   车道线在空间域依然有效**（x 与车速无关），照发；模型线被遮挡由 probs
   自然 null 化，这正是 probs 判线比「整段消失」更好的原因。

### 4.5 leads 推导

规则同 webui，rev2 修正符号与字段写法：

- 过滤：`l.prob ≥ 0.3`（**标量**，非 `prob[0]`）且 `2.0 < l.x[0] < 150.0`；
  按 `x[0]` **显式升序排序**后取 ≤3 个——`leadsV3` 恒 3 槽且槽序≠距离序
  （`fill_model_msg.py:119`，probTime 0/2/4s 三时域槽），不排会乱序；
- `d`/`y`：取 `x[0]`/`y[0]`（t=0 当前相对位），**y 取负输出左正**；
  `v`/`a`/`xStd`/`yStd` 取各自 `[0]`（v 是绝对速度模，不翻转）；
- `heading`：前两样本 `(dx, dy) = (x[1]-x[0], -(y[1]-y[0]))`（先转左正再算），
  `heading = atan2(dy, dx)`——数值上与 webui 的 negation 一致（源同为右正）；
  样本间隔 2s（`LEAD_T_IDXS`），语义是**相对运动方向**而非车头朝向：
  0=同向远离，±π/2=横穿，|h|>π/2=对向——契约写死此语义（D6'）；
- 样本数 <2 或差值≈0 → `heading: null`。

### 4.6 客户端消费侧（rev2 已实核 carrotmate 代码）

- `road.lines/edges/probs` → 透视车道几何；`road.leads` → 模型侧多目标
  （补本仓 radar `leadLeft/Right` 恒空的缺口，`poller.py:200-204` 已核实）；
- **未知段容错已验证**：`StreamJson { ignoreUnknownKeys = true }`
  （`StreamModels.kt:23-27`）——老版本客户端收到 road 静默忽略，§5 兼容性成立；
- **客户端需改的两处**（属 carrotmate 侧工单，接口评审时对照）：
  1. `FrameData`/`SnapshotData` 加 `road` 字段 + `RoadData` 数据类 + 采样/绘制接线；
  2. `SNAPSHOT_SEGMENT_KEYS` 白名单（`ServerMessage.kt:77`）加 `"road"`——该集合
     用于快照信封单层/双层形状判定，注释「段键集合固定」是失效假设，需同步；
- 场景约定匹配：SR 场景左正横偏（`SRSceneCanvas.kt:186`），radar yRel 原生直用，
  road 同样直用；**heading 若客户端按车头朝向渲染需按 D6' 语义注记换算**；
- 现俯视 `scaleMaxDistanceM=100`（`SRSceneConfig.kt`）——栅格 120/160 两点
  在透视升级落地前不可见，不报错（幂压缩自然截断），D1 保留 7 点还是 v1 砍到
  100m 见决策表；
- 引导路径继续吃 dmodel.path（客户端 `pathPoints()` 已实现按 t 求值），**不依赖本段**；
  dmodel 绘制层已有右正取反惯例，road 不要二次翻转；
- 车道线样式（虚实/黄白）由客户端本地高德流（NavRelayBus，已存在）自合成，
  服务端不掺——这正是 §0 审计与 API.md v2→v3 移除 navi 的同一条原则。

## 5. 版本与文档动作

| 项 | 内容 |
|---|---|
| 协议版本 | `PROTOCOL_VERSION` 保持 **6**，API.md 记 **v6.3 additive**（先例：v6.1/v6.2） |
| API.md 更新点 | 头部版本行加一句 v6→v6.3；§4.5 frame 段出现规则加 road；§5 快照段表加 road；§7 流量注记 frame 峰值 ~0.6KB/s → **~2KB/s**（2Hz×~0.7KB）；§12.5 对接清单加「道路几何」行 |
| 顺带修文档债 | API.md §5.3 标题「18 段」与表内实际 19 行不符（`server.py:44-47` 已注记「以段名列表为准」但未修文）——本次一并改 **19→20**，标题/正文/`server.py` 注记三处统一，防计数漂移再犯 |
| 兼容性 | 老客户端按「未知 type/未知段忽略」约定静默兼容（客户端 `ignoreUnknownKeys` 已核实），无需升级 |

## 6. 明确不做

1. **不发 path/pathStd 序列**：ModelDataV2 无此字段，且 carrotmate 已消费
   dmodel.path，双通道同源不同形必生歧义；
2. **不发 line_kinds / laneCurrent / 任何 carrot 导航字段**：客户端回流数据，
   回声禁令（API.md §0）；
3. **不推逐点原始序列**：体积大、客户端还得自己做插值，违背「采样在服务端、
   语义在数据面」的既有分工；
4. **不加新 kind、不动鉴权/门控/params**：本提案纯数据面下行扩展；
5. **不依赖 `t` 字段**：LINE_T_IDXS（到达各距离的时刻，plan 不够远时钳 10s/NaN）
   对 SR 绘制无用，road 段只取空间域 y——这也让 rev1 的「时间域」顾虑整体出局。

## 7. 测试清单（rev2 重写）

- 新增 `road_sample` 单测：正常行驶帧 4 线 2 缘形状正确；
  `laneLineProbs[i] < 阈值` → 该线 null 其余正常；`modelV2.valid=false` →
  整段不发；**静止帧不再断言段消失**（改断言照常下发）；
- **符号锚点测试（防双翻抵消）**：必须用方向地面真值而非对称断言——
  构造左线样本（源 y[0] < 0，右正域）→ 期望输出 `lines[i][0] > 0`（左正域）；
  heading 用 radard 同款换算 `yRel = -y[0]` 做基准（构造横穿样本，源右正域
  dy<0 → 期望 heading>0）。rev1「源=期望同域」的写法在源/测试同时翻错时全绿；
- leads：`prob` 标量阈值过滤、**槽序≠距离序时显式升序**（乱序槽构造用例）、
  两样本差值≈0 → `heading: null`；
- frame/snapshot 同源：同一模型帧，`road` 在 frame 与 `/state` 中逐字段一致
  （car/radar 已有同款测试可参照）；
- 回归：老订阅（不含 road）客户端收帧行为不变；`frame_payload` 既有断言更新。

## 8. 待定决策点（rev2 修订；D2/D5 作废，D5'/D6' 新增）

| # | 点 | 默认值（推荐） | 备选 |
|---|---|---|---|
| D1 | 距离栅格 | webui 同款 7 点 `[5,15,30,50,80,120,160]`（尾部两点为透视预留，现俯视 100m 截断不报错） | v1 砍至 `[5,15,30,50,80]` 贴合现场景，透视落地再扩——但定死即契约，改动即版本注记 |
| ~~D2~~ | ~~尾值钳位 vs null~~ | **作废**：栅格 160 < x[-1]=192，永不越界，两选项恒等价 | — |
| D3 | `roadEdgeStds` | 一期不发（x 静态栅格下 std 只用于降级渲染，probs 已够） | 加 2×7 个数，带宽 +~10% |
| D4 | leads 数量/阈值 | ≤3、`l.prob ≥ 0.3`（标量；webui 实测值）、x[0]∈(2,150)、按 d 显式升序 | 真车再校准 |
| ~~D5~~ | ~~静止退化阈值~~ | **作废**：x 与车速无关，`x[-1]<20` 恒假 | — |
| D5' | 逐线 null 化 probs 阈值 | **0.5**（LDW 同量级置信度；被遮挡时 probs 自然跌落，比「整段消失」优雅） | 0.3 / 0.7，真车观感定 |
| D6' | heading 语义注记 | 契约写死「2s 窗相对运动方向」；客户端若需车头朝向自行换算 | 改样本对为 (x[0],x[1]) 之外——收益为零，不建议 |

## 9. 风险与真车验证项（rev2 重写）

- **符号双翻**（rev2 新增，本提案第一风险）：源右正→输出左正的取负若在服务端
  漏做、或客户端对 path 的既有取反误扩到 road，车道线左右镜像且「看起来仍是车道线」，
  offroad 的 `/state` 肉眼难辨——§7 符号锚点测试必须落地，真车核对时以
  「左实线在左」目视作最终判据；
- **dmodel.laneLineMeta 已埋同款**：`leftY = lane_lines[1].y[0]` 是右正原值
  （正常 ≈ **-1.8**，`fill_model_msg.py:54`）。carrotmate 若已按「左应为正」消费
  leftY，符号 bug 已在现网快照路径存在——客户端工单顺带核对一项；
- **帧对齐**：road 随 frame 2Hz 取「窗口内最新 model 帧」，与同帧 car.vEgo
  可能差 ≤50ms，客户端已有元素级陈旧淡出 + dead-reckoning 兜底
  （`SRStaleFlags`/`interpWindowMs=500`），无需服务端锁步；
- **线数**：本仓 laneLines 恒 4 线（`modelV2.init('laneLines', 4)` 硬编码），
  外侧两线（0/3）probs 常低→null 属预期，客户端按 null 容错即可（rev1「2 还是 4
  看模型配置」修正：形状恒 4，内容看 probs）；
- **probs 阈值观感**：D5' 的 0.5 需真车在隧道/雨天/逆光场景核对 null 化频率，
  过抖可加 2 帧滞回（服务端一行，不改契约）。

---

*实现顺序建议（rev2）：§4.4/4.5 采样函数（含 y 取负）+ 符号锚点单测 →
frame/snapshot 接线（§4.1 清单）→ API.md 同步（含 §5.3 计数债一并修）→
设备 offroad 验证（`/state` 见 road 段、`/ws` 收 frame.road）→
通知 carrotmate 开工（客户端工单：road 数据类 + SNAPSHOT_SEGMENT_KEYS 白名单
+ leftY 符号顺带核对 + 路带标注红线共存）。*

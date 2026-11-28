# carrotmate × mate v6.3：`road` 段适配指南（2026-11-28）

> 读者：carrotmate（Android SR 客户端）开发者。服务端实现见
> `openpilot/mate/poller.py`（`road_sample`/`RoadLineGate`）与 API.md §4.5/§5.4；
> 契约推导与双侧核对记录见 `road-segment-plan-2026-11-28.md`（rev2）。
> 本文自包含：读完即可开工客户端接入，无需回读 openspec 产出物。

## 1. 一段话契约

onroad 时 `frame` 新增 `road` 段（2Hz last-wins，快照 `GET /state` 同步有同名段）：
服务端把 modelV2 的车道线/路缘在**恒定栅格** `dists=[5,15,30,50,80,120,160]` 上采样
成 7 点横偏（左正）、附带高程（z）、逐线置信度、**自车道中心线**、规划不确定度和
过滤排序后的模型侧目标。**服务端已做完采样/符号/有效性**，客户端拿到即画。
老客户端 `ignoreUnknownKeys` 静默兼容，无需同步升级。

## 2. 字段表（详义见 API.md §5.4）

```jsonc
"road": {
  "dists": [5, 15, 30, 50, 80, 120, 160],   // 恒定，可硬编码/缓存
  "lines":  [[...7], null, [...7], [...7]], // laneLines 横偏，左正；整线 null=被剔除
  "edges":  [[...7], null],                 // roadEdges；形状恒 2，缘无逐线 prob
  "heights":[[...7], null, [...7], [...7]], // laneLines.z 原样（相对高程，语义待真车确认）；与 lines 同步 null
  "probs":  [0.95, 0.9, 0.4, 0.1],          // 与 lines 同帧（快照 model 段的是快照域）
  "laneCenters": [0.02, null, ...],         // 自车道中心线@栅格；单侧线 null 仍存活；不可构造点 null
  "pathStd": [0.1, 0.14, ...],              // 规划横向 std（position.yStd 采样）；低速可 null
  "leads": [{ "d": 31.2, "y": 1.9, "prob": 0.71, "v": 12.4, "a": -0.3,
              "xStd": 1.1, "yStd": 0.4, "heading": 0.02 }],   // ≤3，按 d 升序
  "frameId": 4612                           // 重复帧识别
}
```

线序 = 模型序：`[0]=外左 [1]=内左 [2]=内右 [3]=外右`（`fill_lane_line_meta` 用 1/2
作左右道线）。外侧两线 probs 常低→null 属预期，按 null 容错即可。

## 3. 符号惯例并存表（本指南最重要一节）

SR 数据源历史上分两派，road 加入后三派并存，**逐源查表，禁止凭直觉**：

| 数据 | 源域 | 到达客户端时 | 绘制处理 |
|---|---|---|---|
| `frame.radar.*.yRel` | car frame 左正 | 左正 | 直用 |
| `frame.road` / 快照 `road`（本段） | device frame 右正 | **服务端已取负，左正** | **直用——禁止再取反** |
| `dmodel.path`（多项式） | device frame 右正 | 右正原值 | 绘制层取反（现有 `xPx(-p.yM,…)` 惯例，`SRSceneCanvas.kt:280`），**勿把该取反扩到 road** |
| `dmodel.laneLineMeta.leftY/rightY` | device frame 右正 | 右正原值 | **leftY≈-1.8 是正常值**（不是 bug！）；消费时按右正解读 |
| `road.heights`（z） | device frame | 原样 | 符号语义未实证，真车核对（立交/坡道方向） |
| `road.leads.heading` | 左正输出域 | atan2 值 | 语义=**2s 窗相对运动方向**（0=同向远离，±π/2=横穿，\|h\|>π/2=对向），非车头朝向；要画车头需客户端换算 |

> 服务端锚点测试已用 `radard.py:164`（`yRel=-y[0]`）地面真值锁死 road 取负——
> 若你在真机上看到「车道线与实况左右相反」，那是新 bug，不是惯例变化，报 mate 侧。

## 4. 客户端工单清单

1. **`FrameData`/`SnapshotData` 加 `road` 字段**（`StreamModels.kt`）：
   新增 `@Serializable data class RoadData(...)`，字段全可空+默认值
   （`ignoreUnknownKeys` 只管未知键，缺键要靠默认值兜——段缺席=null）。
2. **`SNAPSHOT_SEGMENT_KEYS` 加 `"road"`**（`ServerMessage.kt:77`）：该集合是快照
   信封单层/双层形状判定的段键白名单，注释「段键集合固定」的假设已被本段终结。
3. **新鲜度与陈旧淡出**：以本地收包时刻判（SR 现行口径，`SRStaleFlags` 同款），
   road 段自身无 ts、禁止依赖设备墙钟。段缺席→隐藏+淡出（沿用 pathFresh 模式，
   阈值参考 `frameStaleMs`）。
4. **重复帧去重**：`frameId` 不变→内容不变（模型卡滞时 mate 仍按窗口重发的情况少，
   但 onroad 偶发），客户端可跳过重绘动画。
5. **`dmodel.laneLineMeta` 符号顺带核对**：若现网把 `leftY` 当「左=正」用了，
   按 §3 表改解读（独立小 PR，与本段无关但同源问题）。

## 5. 绘制注记（移植 mouxancnb/webui `road_lite.js` 时）

- **曲线平滑用 Catmull-Rom**（webui `sampleCurve`），7 点栅格直接折线连线会有
  肉眼折角；服务端只保证插值正确，平滑是绘制层职责。
- **目标分类启发式整表可搬**：webui `classifyTarget`（cereal 无类别字段，纯靠
  运动学+std 代理）阈值与 `OBJ_SIZE`：

  ```
  radarOnly → 一律 CAR（radar 无尺寸/不确定度证据，禁止臆造行人）
  xStd > 1.6 → TRUCK；v<2.2 且（|y|>3.0 或 d<26）且 vEgo<70kph → PED
  v<10.5 且 yStd>0.75 且 xStd<1.0 → BIKE(v<5.6)/EBIKE；v<13 且 xStd<0.7 且 yStd<0.6 → MOTO
  其余 → CAR；OBJ_SIZE[l,w]m：CAR[1.85,4.6] TRUCK[2.5,11] BIKE[0.7,1.8] EBIKE[0.8,1.9] MOTO[0.85,2.15] PED[0.6,0.6]
  ```

  输入字段（`v/xStd/yStd/d/y`）road.leads 契约全有。
- **概率双滤**：服务端 0.5 已先行整线 null；webui 前端 `LINE_PROB_MIN=0.35`
  可保留作兜底（过 0.5 必过 0.35，不冲突），透明度建议 ∝ probs 延续。
- **地平线**：webui 用常量 `HORIZON=0.42`（无相机 pitch）——起步够用；
  增强项见 §6 calib 行（车机支架俯仰角会变，动态地平线防「穿地/浮空」）。

### 明确不抄 webui 的三件事

1. **无数据回退假直路**（webui `None → falls back to a straight road`）：违
   carrotmate sr-view「禁止假想补画」红线——段缺席就隐藏，不画想象线。
2. **`line_kinds`/`line_types`**：那是 CarrotNaviSP 回声（API.md §0 回声禁令，
   mate 侧不下发）；车道样式用你本地 NavRelayBus 高德流合成，信息比 webui 的
   blocked 二值全（webui 只有实/虚，你有黄白）。样式枚举可参照 webui 的
   `LK_*` 7 值（0 unknown·1 白实·2 白虚·3 黄实·4 双黄·5 圆点·6 路缘）做本地映射。
3. **leads 的 `i` 槽位号**：webui 发了但其前端从未消费，mate 不发，别等。

## 6. 快照既有字段的 SR 增值（零服务端成本，均已下发未消费）

| 字段（快照段） | SR 用途 | 备注 |
|---|---|---|
| `model.sp.blendfulLaneWidth` | 车道宽真值 | 替换 `SRSceneConfig.halfLaneWidthM=1.8` 常量，窄道/收费站不再画错道宽；回退 1.8 |
| `calib.rpyCalib` / `wideFromDeviceEuler` | 动态地平线/相机 pitch | 透视第一帧不穿帮的刚需；4Hz 快照，慢随动即可 |
| `model.confidence`（green/yellow/red） | 全局线淡化强度 | 比 probs 硬阈值更优雅；黄/红时整体线透明衰减 |
| `model.sp.blendfulLeftProb/RightProb/GoingAround` | 变道意图辉光 | 连续概率早于离散 `laneChangeDirection`，目标车道线渐进高亮 |
| `model.hardBrakePredicted` | FCW 前车红脉冲前兆 | 比 radar 告警快 |
| `odom.rot` | 换道镜头 yaw 平滑 | 2Hz dead-reckoning 补不了旋转 |

## 7. 边界（别等服务端的东西）

- **192m 外道路延续**：模型栅格止于 160m（x 网格 192m），远端接路线用本地高德
  polyline+曲率；`POST /route_geometry` 是**上行**喂 op 曲率预览的，无下行镜像。
- **行人/两轮真实检测**：本仓 cereal 无 yolo/objectSensor/trafficLight 消息——
  目标分类只有 §5 启发式一条路。红绿灯状态在 `frame.carrot.trafficState`（0-3）。
- **天气/昼夜氛围**：无 roadScene 消息，客户端拿系统时间+位置自合成。
- **驾驶员状态**：`DriverMonitoringState` 存在但 mate 未订阅；需要时走 mate 侧
  新提案（v6.4 候选段）。

## 8. 联调验收路径

1. 车机起 mate 后浏览器开 `http://<车机IP>:8083/ui/`（状态页）：
   **道路几何卡就是服务端验收图**——上=前、左=自车左，绿点线=车道中心；
   符号错误在页面上肉眼可见（左线画右侧）。
2. 「视频」页开 road 前摄实况，与 road 卡左右对照（同一帧时刻 ±50ms）。
3. 客户端接入后：真车首看项——左实线在左、`heights` 坡向、蠕行时线不消失、
   隧道口 probs 跌落线淡出（无闪现）。
4. 客户端模拟器：`SRSceneSimulator`/`SRSampleData` 已有基建，按 §2 JSON 形状
   造四段剧本（右弯+坡道+横穿 lead+probs 跌落）可全离线验证。

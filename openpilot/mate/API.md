# mate 接口文档

车机事件实时推送服务。订阅 cereal 消息总线，把告警事件、openpilot 状态机跳转、
方向盘按键、播报声音四类**边沿事件**与 2Hz 的 **frame 周期状态帧**（car/radar 段）
通过 **WebSocket** 或 **SSE** 推给局域网客户端，并提供一次性状态快照 `GET /state`。

> **协议版本：v5**（`hello.version = 5`）。v2 → v3 迁移要点见 §9；v3 → v4 变更：`frame` 新增 `carrot` 段（carrotMan 遥测）；v4 → v5 变更：`GET /state` 与 WS `snapshot` 扩展 18 个纯快照段（§5.3），kind 白名单、推送语义与鉴权均不变；v5 追加：`hello` 新增 `flavor` 字段（fork 标识，本仓为 `sunnypilot`）。

## 0. 服务定位原则与架构

**mate 只推「op 能获取、而局域网客户端无法自产」的数据；数据源在客户端侧的数据
禁止回推。** v2 的 `navi` kind 即因此移除——其输入 `carrotNavi` 是 carrotmate
（Android 车机端）自己推给 op 的导航数据，回推属回声。新增 kind 一律先过此审计。

设备拓扑与数据流（两条链路互不相干）：

```
┌──────────────── Android 车机大屏（carrotmate 运行处）────────────────┐
│  高德地图车机版 ──全局广播──▶ carrotmate                              │
└──────────┬──────────────────────────────────┬───────────────────────┘
           │ 导航上行（7706 UDP / 7714 WS）     │ 状态下行（8083 WS）
           ▼                                  ▼
┌─────────────────────┐          ┌──────────────────────────────┐
│ CarrotPilot 设备     │          │  mate :8083（本服务）          │
│  carrot_navi.py      │          │  只推 op 独有数据：             │
│   → carrotNavi       │          │   events/opstate/button/sound │
│   → op 内部消费       │          │   + 2Hz frame（car/radar）     │
│  （HUD/soundd/规划）  │          └──────────────────────────────┘
└─────────────────────┘
```

- 上行（导航）：高德 → carrotmate → `carrot_navi.py` → cereal `carrotNavi` →
  op 内部（HUD / soundd / 规划）。**mate 不参与，也不回推**。
- 下行（状态）：mate 订阅 cereal → WS/SSE → 客户端。

- 服务代码：`openpilot/mate/server.py`（事件推导在 `openpilot/mate/poller.py`）
- 默认端口：**8083**（env `MATE_PORT` 可覆盖）
- 数据格式：JSON（UTF-8）
- 进程注册：manager `always_run`，开机常驻（offroad 也可连）

---

## 1. 接入

### 1.1 端点

| 端点 | 协议 | 鉴权 | 说明 |
|---|---|---|---|
| `ws://<车机IP>:8083/ws` | WebSocket | ✔ | 双向：收事件与 frame，可发 subscribe / snapshot / ping |
| `http://<车机IP>:8083/stream` | SSE | ✔ | 单向推流，`?kinds=events,sound` 过滤 |
| `http://<车机IP>:8083/state` | HTTP GET | ✔ | 一次性全量状态快照：数据段 + 服务状态段（见 §5） |
| `http://<车机IP>:8083/sounds/<file.wav>` | HTTP GET | ✔ | sound 事件对应音效文件 |
| `http://<车机IP>:8083/health` | HTTP GET | ✘ | 服务健康状态（含 poller 心跳新鲜度） |

车机 IP 在车机设置 / 路由器管理页查看，客户端需与车机在同一局域网。

### 1.2 鉴权（可选）

默认不校验。写入 token 后，除 `/health` 外所有接口必须携带：

```bash
echo -n "your-secret" > /data/params/d/MateToken    # 开
rm /data/params/d/MateToken                          # 关
```

改完约 1 秒内生效（服务端缓存），无需重启进程。客户端三种方式任选：

- Query 参数：`ws://host:8083/ws?token=your-secret`
- 请求头：`Authorization: Bearer your-secret`
- SSE 用 query 参数（浏览器 EventSource 不支持自定义头）

鉴权失败语义（v3）：

- **WS**：连接升级成功 → 服务端发 `{"type":"error","data":{"msg":"invalid token"}}` →
  关闭连接。客户端应按消息语义处理（如显示"配对失败"），而非笼统的连接失败重试。
- **SSE / HTTP**：HTTP 401 `{"error":"invalid token"}`。

### 1.3 事件类型（kind）

| kind | 触发时机 | 进 /state 快照 | 新连接补发 |
|---|---|---|---|
| `events` | onroad 告警集合**变化**（去抖） | ✔ | ✔（有活跃告警时） |
| `opstate` | openpilot 状态机跳转 / enabled/active/engageable 变化 | ✔ | ✘ |
| `button` | 方向盘按键按下/松开（边沿帧） | ✘ | ✘ |
| `sound` | 播报声音切换（含 carrot 倒计时报数） | ✔ | ✔（正在播报时） |
| `frame` | **2Hz 周期状态帧**（car/radar 段，驾驶屏连续值） | ✘（段进快照） | ✘（走快照） |

> v5 起 kind 集不变；扩展全部落在 `/state` 与 WS `snapshot` 的状态段上（§5.3）。

---

## 2. WebSocket 协议

### 2.1 服务端 → 客户端

所有消息统一信封：

```json
{ "type": "<类型>", "data": { ... } }
```

| type | 触发时机 | 说明 |
|---|---|---|
| `hello` | 连接建立后第一条 | `{"service":"mate","version":5,"flavor":"sunnypilot","port":8083,"kinds":[...]}`；`version` 供客户端探测能力，`flavor` 标识上游 fork（`sunnypilot` / `carrotpilot`），客户端据此区分数据面差异（如 `carrotMan` 与 SP 段的有无） |
| `events` / `opstate` / `button` / `sound` / `frame` | 见 §4 | 事件/状态本体 |
| `snapshot` | 响应 snapshot 请求 | 见 §5 |
| `subscribed` | 响应 subscribe 请求 | `{"kinds":[...]}` 当前生效过滤 |
| `error` | **鉴权失败**（v3 新增） | `{"msg":"invalid token"}`，随后连接关闭 |
| `pong` | 响应 ping | `{"ts": <monotonic>}` |

> **版本约定**：`hello.version` 升级代表 kind 集或语义变化。客户端**必须忽略未知
> type**（未升级的客户端连 v3 服务会多收到 `frame` 消息，按此约定静默忽略即可），
> 据此实现前后向兼容。

连接建立后依次收到：`hello` → （当前活跃告警的 `events` 补发，有则发）→
（当前正在播报的 `sound` 补发，有则发）。**按键与 frame 不补发**——客户端拿当前
车辆/雷达状态请用 `snapshot` / `GET /state`。

服务端每 20s 发 WebSocket protocol ping，死连接自动清理。

### 2.2 客户端 → 服务端

```jsonc
// 订阅过滤（不发送则默认全部 5 类；重复发送覆盖之前的选择）
{ "type": "subscribe", "kinds": ["events", "sound"] }

// 请求一次全量状态快照（立即返回，不影响后续推送）
{ "type": "snapshot" }

// 心跳 / 测延迟
{ "type": "ping" }
```

订阅规则：白名单外的 kind 被忽略；`kinds` 缺省或全为非法值时回退全量白名单。
未知消息与非法 JSON 直接忽略（不报错），便于客户端版本迭代。

### 2.3 JavaScript 示例

```javascript
const ws = new WebSocket("ws://192.168.1.100:8083/ws?token=your-secret");

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  switch (msg.type) {
    case "hello":
      // 初始化：拿一份快照补齐当前状态
      ws.send(JSON.stringify({ type: "snapshot" }));
      break;
    case "snapshot":
      console.log("当前 OP 状态:", msg.data.data.opstate?.data.state);
      console.log("车速 km/h:", msg.data.data.car?.data.vEgoKph);
      break;
    case "events":
      for (const n of msg.data.added) console.log("新告警:", n);
      for (const n of msg.data.removed) console.log("告警解除:", n);
      break;
    case "opstate":
      console.log("OP:", msg.data.state, "active =", msg.data.active);
      break;
    case "button":
      console.log("按键:", msg.data.button, msg.data.pressed ? "按下" : "松开");
      break;
    case "sound":
      console.log("播报:", msg.data.sound, msg.data.loop ? "循环" : "单次");
      break;
    case "frame":
      // 2Hz 驾驶屏连续值：车速/挡位/巡航/前车距离（last-wins，丢帧无害）
      if (msg.data.car) console.log("车速:", msg.data.car.vEgoKph);
      if (msg.data.radar) console.log("前车距离:", msg.data.radar.leadOne?.dRel);
      break;
    default:
      // 未知 type 一律忽略（协议向前兼容的关键约定）
      break;
  }
};
```

### 2.4 Python 示例（websocket-client，仓库已依赖）

```python
import json
import websocket

def on_message(ws, message):
    msg = json.loads(message)
    if msg["type"] in ("events", "opstate", "button", "sound", "frame"):
        print(msg["type"], msg["data"])

ws = websocket.WebSocketApp("ws://192.168.1.100:8083/ws?token=your-secret",
                            on_message=on_message)
ws.run_forever()
```

---

## 3. SSE 协议

- 地址：`http://<车机IP>:8083/stream?kinds=events,sound&token=your-secret`
  （`kinds` 省略 = 全部；非法 kind 被忽略）
- SSE 的 `event:` 名即消息类型（`events`/`opstate`/`button`/`sound`/`frame`），
  `data:` 为同样的 JSON 信封
- 新连接同样先补发活跃告警与正在播报的声音（frame 不补发）
- 空闲 15s 输出一条 `: ping` 注释行保活
- 慢客户端：队列上限 256，满了丢最旧（frame 为状态语义，丢旧保新无害）
- 浏览器 `EventSource` 断线自动重连

```bash
curl -N "http://<车机IP>:8083/stream?kinds=events,opstate"
```

---

## 4. 事件与帧数据字典

### 4.1 `events` 告警事件

onroad 告警集合**发生变化时**推送（出现/消失去抖，不每帧重复）：

```json
{
  "type": "events",
  "data": {
    "added":   ["doorOpen", "seatbeltNotLatched"],
    "removed": ["belowSteerSpeed"],
    "active":  [ { "name": "doorOpen", "warning": true, "...": "..." } ]
  }
}
```

`active` 每项字段（与本仓 `cereal/log.capnp` `OnroadEvent` 一致）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `name` | string | 事件枚举名（完整列表见 `OnroadEvent.EventName`） |
| `warning` | bool | 黄/红警告（enabled 时展示） |
| `userDisable` | bool | 用户操作导致退出（踏板/转向/按键接管） |
| `softDisable` | bool | 软退出（可自动恢复） |
| `immediateDisable` | bool | 立即退出 |
| `permanent` | bool | 常驻告警（无论 OP 状态都展示） |
| `noEntry` | bool | 阻止 engage（解释了"为什么按了没反应"） |
| `preEnable` | bool | pre-enable 阶段提示 |
| `enable` | bool | 事件本身使能 |
| `overrideLateral` | bool | 横向被接管 |
| `overrideLongitudinal` | bool | 纵向被接管 |

> 粒度说明：事件集合变化即推，**同一事件仅 flag 变化不会重推**；客户端要拿
> 最新 flag 以 `active` 全量为准。BYD 上 `onroadEvents` 由 selfdrived 发布，
> 边沿粒度约一个发布周期（100ms 级）。

### 4.2 `opstate` 状态机跳转

`state` / `enabled` / `active` / `engageable` 任一变化时推送：

```json
{ "type": "opstate", "data": { "state": "enabled", "enabled": true, "active": true, "engageable": false } }
```

| state | 含义 |
|---|---|
| `disabled` | 未开启 |
| `preEnabled` | 预开启（踩刹车待命中） |
| `enabled` | 已开启（在控车，`active=true`） |
| `softDisabling` | 软退出中（可自动恢复） |
| `overriding` | 驾驶员接管中（油门/转向） |

### 4.3 `button` 方向盘按键

边沿事件，按下/松开各一条；长按不重复发：

```json
{ "type": "button", "data": { "button": "setCruise", "pressed": true } }
```

| `button` 值 | 含义 | | `button` 值 | 含义 |
|---|---|---|---|---|
| `mainCruise` | 巡航总开关 | | `gapAdjustCruise` | 跟车时距 |
| `setCruise` | SET− 设定 | | `lkas` | LKAS 按钮 |
| `resumeCruise` | RES+ 恢复 | | `lfaButton` | LFA 按钮（部分车型） |
| `accelCruise` | 巡航加速 | | `leftBlinker` / `rightBlinker` | 转向拨杆 |
| `decelCruise` | 巡航减速 | | `paddleLeft` / `paddleRight` | 换挡拨片 |
| `cancel` | 取消巡航 | | `altButton2` / `unknown` | 备用 / 未识别 |

> 转向灯持续状态请用 frame/快照的 `car.leftBlinker/rightBlinker`；
> 此事件是拨杆的**瞬间动作**。BYD 宋PLUS 注意：下拨 SET 厂商即激活（SET 是激活语义
> 而非设定，属厂商行为），客户端别把 `setCruise` 按下直接映射成"设定巡航"文案。

### 4.4 `sound` 播报声音

与本机 `soundd` 的发声判定完全同链（含 carrot 倒计时报数、巡航主开关长按提示）：

```json
{
  "type": "sound",
  "data": {
    "sound": "stopStop",
    "file": "audio_stopstop.wav",
    "loop": true,
    "alertText1": "BRAKE!",
    "alertText2": "Risk of Collision"
  }
}
```

| 字段 | 说明 |
|---|---|
| `sound` | `AudibleAlert` 枚举名；`none` 表示播报结束 |
| `file` | wav 文件名，可 `GET /sounds/<file>` 拉取；`none` 时为 `null` |
| `loop` | true=循环播到下一条 `sound`；false=播一遍 |
| `alertText1/2` | 车机屏幕上同步显示的告警文字（可能为空串） |

常见值（完整枚举见本仓 `opendbc/car/car.capnp` `AudibleAlert`）：
`engage`/`disengage`/`refuse`/`prompt`/`promptRepeat`/`promptDistracted`/
`warningSoft`/`warningImmediate`/`stopStop`/`bsdWarning`/`stopping`/
`trafficSignGreen`/`trafficSignChanged`/`laneChange`/`audioTurn`/`reverseGear`/
`longEngaged`/`longDisengaged`/`autoHold`/`engage2`/`disengage2`/`speedDown`/
`trafficError`/`radarCutin`/`systemReady`/`audio1`…`audio10`（倒计时报数）。

实现细节：
- `file` 来自本仓 `soundd.py` 的 `sound_list`，随设备型号（tizi 的
  `engage_tizi.wav` 等）与语言设置（`SoundLanguageSetting`/`LanguageSetting` →
  `sounds`/`sounds_chs`/`sounds_eng`）自动切换
- carrot 倒计时合成规则与 soundd 一致：`leftSec` 10→1 报数、0 → `longDisengaged`、
  11 → `promptDistracted`
- **与 soundd 的唯一差异**：soundd 会等有限长音效播完才转 `none`，mate 没有音频时钟，
  推导结束即推 `none`（客户端只会提前停播，不会漏停）

### 4.5 `frame` 周期状态帧（v3 新增；v4 增加 `carrot` 段）

**2Hz 周期推送** `{car?, radar?}`，喂驾驶屏的连续值（车速/挡位/巡航/前车距离）。
段与快照 `car`/`radar` 段**同源同函数**——一个拉（快照）一个推（frame），结构一致。
last-wins 语义：允许丢帧（客户端直接以最新帧覆盖状态即可），offroad（数据源停止）
自动停推。

```json
{
  "type": "frame",
  "data": {
    "car":   { "vEgoKph": 54.0, "gearShifter": "drive", "...": "..." },
    "radar": { "leadOne": { "status": true, "dRel": 32.5, "...": "..." } }
  }
}
```

段出现规则：仅包含有数据的段——`radarState` 从未见数据（offroad 启动初期）时
`radar` 段不出现；两段都无更新（offroad）则整帧不推。

#### `car` 段字段

见 §5 `car` 段字段表（与快照完全一致，含 v3 新增的 `fuelGauge`/`charging`）。

#### `radar` 段字段

取自 `radarState`（radarld 发布，100Hz，frame 取最新值）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `leadOne` / `leadTwo` | Lead 对象 | 本车道前车一/二目标 |
| `leadLeft` / `leadRight` | Lead 对象 | 左/右邻车道目标（Carrot 扩展跟踪） |
| `leadsCenterCount` | int | 本车道中心跟踪目标数 |
| `radarErrors` | string[] | 非空雷达错误名列表：`canError`/`radarFault`/`wrongConfig`/`radarUnavailableTemporary` |

Lead 对象（字段名与 carrotmate `Lead` 模型一致，`trackId` 即 cereal
`radarTrackId`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | bool | 该目标是否有效 |
| `dRel` | float | 纵向距离 m（前正后负） |
| `yRel` | float | 横向偏移 m（左正右负） |
| `vRel` | float | 相对速度 m/s（负=接近） |
| `aRel` | float | 相对加速度 m/s² |
| `vLead` | float | 前车绝对速度 m/s |
| `dPath` | float | 沿规划路径的纵向距离 m（dPath 雷达管道） |
| `fcw` | bool | 前碰撞预警标志 |
| `trackId` | int | 雷达轨迹号（跨帧跟踪身份） |

典型用法：`leadOne.dRel` → 前车距离条；`vRel<0` 持续 → 接近趋势；
`leadLeft/leadRight.status` → 邻车道车可视化。

> **本仓差异**：本仓 radarState 仅发布 `leadOne`/`leadTwo`，`leadLeft`/`leadRight`
> 恒为空 lead、`leadsCenterCount` 恒 0；Lead 的 `aRel`/`dPath`/`fcw` 恒 0/False
> （本仓 `RadarState.LeadData` 无这些字段）。键位保留以兼容既有客户端模型。

#### `carrot` 段字段（**v4 新增**）

取自 `carrotMan`（carrot_man 20Hz 聚合输出，frame 取窗口内最新值）。
`carrot_man` 进程未运行（或未开搭子）时该段不出现。

| 字段 | 类型 | 说明 |
|---|---|---|
| `nRoadLimitSpeed` | int | 当前道路限速 km/h（AMap road 源，0=无效） |
| `xSpdLimit` | int | 当前生效的测速/电子眼限速 km/h（0=无） |
| `xSpdDist` | int | 距测速点距离 m |
| `xSpdCountDown` | int | 测速倒计时 s |
| `xDistToTurn` | int | 距下一转向距离 m |
| `xTurnCountDown` | int | 转向倒计时 s |
| `desiredSpeed` | int | carrot 多源最小决策目标速度 km/h |
| `desiredSource` | string | 胜出来源（road/sdi/atc/vturn…） |
| `szPosRoadName` | string | 当前路名（高德） |
| `leftSec` | int | 测速倒计时秒（驱动 1-10s beep，0=长切告警） |
| `trafficState` | int | 红绿灯状态 0-3（无/红灯/绿-将停/绿灯） |

典型用法：手机端 SpeedLimitDetector/语音包告警改吃本段（v4 APK），不再依赖
`car.speedLimitKph`（BYD 上恒 0）。

---

## 5. `GET /state` 与 WS `snapshot`

一次性返回当前全量状态快照（两者数据体相同）：

```json
{
  "ts": 1791472951.467,
  "data": {
    "car":      { "ts": 1791472951.400, "data": { "vEgoKph": 54.0, "...": "..." } },
    "radar":    { "ts": 1791472951.410, "data": { "leadOne": { "...": "..." }, "...": "..." } },
    "opstate":  { "ts": 1791472940.123, "data": { "state": "enabled", "...": "..." } },
    "events":   { "ts": 1791472947.925, "data": { "added": [...], "removed": [...], "active": [...] } },
    "sound":    { "ts": 1791472948.300, "data": { "sound": "stopStop", "...": "..." } },
    "panda":    { "ts": 1791472951.380, "data": { "count": 1, "pandas": [ "..." ] } },
    "device":   { "ts": 1791472951.405, "data": { "networkType": "cell4G", "maxTempC": 46.1, "...": "..." } },
    "carParams": { "ts": 1791472000.000, "data": { "brand": "byd", "carFingerprint": "...", "sp": { "...": "..." } } },
    "controls": { "ts": 1791472951.410, "data": { "state": "enabled", "alertText1": "...", "...": "..." } },
    "plan":     { "ts": 1791472951.415, "data": { "aTarget": -0.52, "sp": { "...": "..." }, "...": "..." } },
    "loc":      { "ts": 1791472951.418, "data": { "posGeodetic": { "value": [31.2, 121.4, 10.0], "...": "..." } } },
    "services": { "ts": 1791472951.420, "data": { "carState": { "...": "..." }, "...": "..." } },
    "meta":     { "ts": 1791472951.420, "data": { "frame": 41233, "onroad": false } }
  }
}
```

> 以上为节选。v5 全部新增段（device/peripheral/manager/proc/backup/temp/carParams/
> controls/carSP/control/plan/loc/gps/map/model/dmodel/odom/calib/params）见 §5.3。

- 每段自带 `ts`（Unix 墙钟秒，车机时钟错时自行对齐），客户端据此判断新鲜度
- **未收到过的数据段不出现**（offroad 刚启动时 `car`/`opstate` 可能缺失；
  `radar` 段只在收到过 radarState 帧后出现）；`button` 永远不在快照里（瞬时边沿）
- `events` 段在告警清空后保留空 `active`（表示"确认无告警"，区别于未收到）
- `meta` / `services` 每轮 poller 循环刷新，`panda` 随 pandaStates（10Hz）刷新，
  `car`/`radar` 随各自数据源（onroad 100Hz 级）刷新；
  这些段是**服务/状态段**，只出现在 `/state` 与 WS `snapshot`，不进事件流广播

### 服务状态段（meta / services / panda）

`data` 为空或缺段时，按这三段自查，顺序如下：

1. **meta.ts 是不是在变旧** → 变旧超过 1–2s = poller 线程已死/卡死（HTTP 服务本身
   仍会正常应答，别信 200）。`frame` 是 SubMaster 循环计数，单调递增，可当心跳序号。
2. **meta.onroad**（params `IsOnroad`）→ `false` 时 `card`/`selfdrived` 不运行，
   `car`/`opstate`/`events`/`sound`/`radar` 缺段属**常态**，不是故障。
3. **services**：每个订阅 topic 一条，字段如下：

| 字段 | 类型 | 含义 |
|---|---|---|
| `seen` | bool | 服务启动以来是否收到过该 topic（频率 0 的 on-demand topic 恒判 `alive`，要靠它区分） |
| `alive` | bool | 最近消息是否在 10× 期望周期内（新鲜度） |
| `valid` | bool | 最近一条消息的 `valid` 标志 |
| `ageS` | float/null | 距最近一条消息的秒数（本进程单调钟差值，**仅同设备同次开机内可比**）；`null`=从未收到 |

`panda` 段来自 pandaStates——`pandad` 常驻，**offroad 下唯一持续更新的数据段**：

| 字段 | 含义 |
|---|---|
| `count` / `pandas[]` | panda 数量与逐只明细（`type` 型号，`uptime` 秒） |
| `heartbeatLost` | true=主机与这只 panda 失联（USB/线路问题） |
| `ignitionLine` | 点火线检测（台架判断 ACC 档位） |
| `controlsAllowed` | panda 是否放行控制输出 |
| `safetyModel` / `safetyParam` | 安全模型（如 `byd`）与其参数 |
| `faultStatus` / `faults[]` | `none`/`faultTemp`/`faultPerm` 与逐条故障枚举 |
| `harnessStatus` | `notConnected`/`normal`/`flipped`（comma  harness） |
| `canStates[]` | 每路 CAN：`busOff`/`busOffCnt` 总线关闭，`canSpeed`（0=该路没流量，台架查接线/车辆上电），`totalRxCnt/totalTxCnt` 收发计数，`errorWarning/errorPassive` 错误状态 |

> 典型判读：offroad 且 `panda.pandas[0].heartbeatLost=false`、`canStates[*].canSpeed>0`
> → 车机与 panda 链路正常，只是 openpilot 没上电；`panda` 整段缺失 → pandad 没跑或
> `services.pandaStates.seen=false`。

### `car` 段字段

与 frame 的 `car` 段完全一致：

| 字段 | 类型 | 单位 | 说明 |
|---|---|---|---|
| `vEgo` / `vEgoKph` | float | m/s, km/h | 最佳估计车速 |
| `vEgoCluster` | float | m/s | 仪表显示车速 |
| `aEgo` | float | m/s² | 估计加速度 |
| `standstill` | bool | | 静止 |
| `gearShifter` | string | | `unknown`/`park`/`reverse`/`neutral`/`drive`/`sport`/`low`/`brake`/`eco`/`manumatic` |
| `gas` / `brake` | float | 0–1 | 踏板开度（仅驾驶员） |
| `gasPressed` / `brakePressed` | bool | | 踏板踩下 |
| `steeringAngleDeg` | float | deg | 方向盘转角 |
| `steeringPressed` | bool | | 手在方向盘上 |
| `cruiseState` | object | speed/speedCluster m/s，`speedKph` km/h | `{enabled, available, speed, speedKph, speedCluster, standstill, nonAdaptive}` |
| `vCruise` / `vCruiseKph` | float | km/h | 巡航设定速度（openpilot `V_CRUISE_*` 语义，本身即 kph；未设定=255，勿再乘 3.6） |
| `leftBlinker` / `rightBlinker` | bool | | 转向灯 |
| `leftBlindspot` / `rightBlindspot` | bool | | 左/右盲区有车（CAN BSM，轮询用） |
| `doorOpen` | bool | | 车门开启 |
| `seatbeltUnlatched` | bool | | 安全带未系 |
| `parkingBrake` | bool | | 手刹 |
| `brakeHoldActive` | bool | | 自动驻车 |
| `latEnabled` | bool | | 横向控制激活 |
| `speedLimit` / `speedLimitKph` | float | m/s, km/h | 限速 |
| `engineRpm` | float | rpm | 发动机转速 |
| `accFaulted` / `espDisabled` | bool | | ACC 故障 / ESP 关闭 |
| `canValid` / `canTimeout` | bool | | CAN 校验 / 掉线 |
| `carrotCruise` | int | | carrot 巡航设定 |
| `fuelGauge` | float | 0–1 | **v3 新增**：油量或电池电量比例（`carState.fuelGauge` 直传；BYD DM-i 上语义按 CAN population 为 SOC 或油位，实测为准） |
| `charging` | bool | | **v3 新增**：充电中（`carState.charging` 直传） |

> 快照在 card 停发（offroad）后保持最后一次 onroad 值，新鲜度看 `car.ts`。

### 5.3 v5 新增快照段（18 段）

v5 在 `/state` 与 WS `snapshot` 中新增以下状态段。与 `meta`/`services`/`panda` 同机制：

- **纯快照段**：只进 `/state` 与 WS `snapshot`，不进事件流广播、不补发
- **出现规则**：对应数据源收到过至少一帧才出现（offroad 未跑的进程其段不出现）
- **组合段**：`carParams`/`control`/`plan`/`model`/`params` 为多 topic 合成，任一来源更新即整体重建
- **体积**：全段齐全时 `/state` 约 15-25KB（`proc` 段占大头，随进程数浮动），按需拉取即可

| 段 | 数据源 topic | 频率 | 内容 |
|---|---|---|---|
| `device` | deviceState | 2Hz | 设备网络/存储/内存/温度/热状态/风扇 |
| `peripheral` | peripheralState | 2Hz | panda 外设：电压/电流/风扇转速 |
| `manager` | managerState | 2Hz | 各进程存活状态（running/shouldBeRunning/exitCode） |
| `proc` | procLog | 0.5Hz | 内存总量 + 逐进程 CPU/内存（运维看板） |
| `backup` | backupManagerSP | 1Hz | sunnypilot 备份/恢复状态与进度 |
| `temp` | temperatureSensor | 2Hz | 传感器温度（匿名 union，未选中时 `temperatureC=null`） |
| `carParams` | carParams + carParamsSP | 0.02Hz | 车辆身份与动力学参数（静态），SP 子对象 |
| `controls` | controlsState | 100Hz | 控制器状态与完整告警字段（取最新） |
| `carSP` | carStateSP | 100Hz | sunnypilot 扩展车辆状态（speedLimit 等） |
| `control` | carControl + carControlSP | 100Hz | op 实际下发执行器指令 + HUD 提示（取最新） |
| `plan` | longitudinalPlan + longitudinalPlanSP + lateralManeuverPlan | 20Hz | 纵向规划序列 + SP 智能巡航/限速辅助 |
| `loc` | liveLocationKalman | 20Hz | EKF 融合定位（经纬/速度/姿态，车机 GNSS+IMU） |
| `gps` | gpsLocationExternal | 10Hz | 原始 GNSS（低精度兜底，与 `loc` 互补） |
| `map` | liveMapDataSP | 1Hz | 在线地图限速/前方限速/路名（mapd 拉取，非客户端上行） |
| `model` | modelV2 + modelDataV2SP | 20Hz | 模型元信息/决策摘要/前车首采样，SP 子对象 |
| `dmodel` | drivingModelData | 20Hz | UI 驾驶模型：路径多项式系数/车道线置信度 |
| `odom` | cameraOdometry | 20Hz | 视觉里程计（速度/角速度 + 标准差） |
| `calib` | extrinsicsCalibration | 4Hz | 相机标定状态/外参/rpy |
| `params` | lateralTorqueParameters + lateralDelay + vehicleParameters | 4-20Hz | 扭矩参数标定/横向延迟/在线动力学估计 |

各段枚举均为字符串（`enum_name`），未知值回退 `"unknown"`，客户端按容错处理。

#### `device` 段字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `networkType` | string | `none`/`wifi`/`cell2G`…`cell5G`/`ethernet` |
| `networkStrength` | string | `unknown`/`poor`/`moderate`/`good`/`great` |
| `networkMetered` | bool | 计费网络 |
| `started` | bool | openpilot 已上电（IsOnroad 语义外的设备 started 标志） |
| `freeSpacePercent` / `memoryUsagePercent` / `gpuUsagePercent` | float/int | 存储剩余/内存/GPU 占用 |
| `cpuUsagePercent` | float[] | 逐核 CPU 占用 |
| `powerDrawW` / `somPowerDrawW` | float | 整机/SOM 功耗 W |
| `cpuTempC` / `gpuTempC` / `memoryTempC` / `maxTempC` | 数值/数组 | 板温明细（℃） |
| `thermalZones` | list | `{name, tempC}` 逐热区温度 |
| `thermalStatus` | string | `ok`/`overheated`/`critical`（本仓无 `warm`） |
| `fanSpeedPercentDesired` | int | 风扇目标转速百分比 |

#### `peripheral` / `manager` / `proc` / `backup` / `temp` 段

- `peripheral`：`pandaType`（枚举，`unknown`/`whitePanda`…`tres`/`cuatro`）、`voltage`、`current`、`fanSpeedRpm`
- `manager`：`processes[] = {name, pid, running, shouldBeRunning, exitCode}`——`shouldBeRunning=true && running=false` 即进程挂了，配合 exitCode 排查
- `proc`：`mem = {totalMb, freeMb, availableMb}` + `procs[] = {name, pid, cpuUser, cpuSystem, threads, rssMb, pssMb}`（CPU 为累计占用百分比，可直接排序取 Top）
- `backup`：`backupStatus`/`restoreStatus`（`idle`/`inProgress`/`completed`/`failed`）、`backupProgress`/`restoreProgress`（0-1）、`lastError`、`currentBackup = {deviceId, version, isEncrypted, createdAt, updatedAt}`
- `temp`：`timestamp`、`source`（传感器枚举）、`temperatureC`——temperatureSensor 的 union 未选中温度分支时为 `null`

#### `carParams` / `carSP` / `controls` / `control` 段

- `carParams`：`brand`、`carFingerprint`、`carVin`、`fuzzyFingerprint`、`passive`、`pcmCruise`、`flags`、`minEnableSpeed`/`minSteerSpeed`/`maxLateralAccel`、`wheelbase`/`centerToFront`/`steerRatio`、`steerControlType`（`torque`/`angle`/`curvature`）、`radarUnavailable`、`steerActuatorDelay`/`longitudinalActuatorDelay`、`openpilotLongitudinalControl`、`transmissionType`、`networkLocation` 等 + `sp = {flags, safetyParam, pcmCruiseSpeed, intelligentCruiseButtonManagementAvailable, enableGasInterceptor}`
- `carSP`：`speedLimit`（sunnypilot 扩展车速上限，m/s，原样直传）
- `controls`：`state`/`enabled`/`active`（与 opstate 段同语义的控制器侧视图）、`alertText1/2`、`alertType`、`alertStatus`/`alertSize`/`alertSound`（完整告警三件套）、`longControlState`（`off`/`pid`/`stopping`/`starting`）、`curvature`/`desiredCurvature`、`forceDecel`/`decelForTurn`/`decelForModel`
  > 本仓 ControlsState 的这些字段在 capnp `deprecated :group` 下（carrot 树遗留布局），mate 已做映射，客户端无感。
- `control`：`enabled`/`latActive`/`longActive`、`leftBlinker`/`rightBlinker`、`currentCurvature`、`actuators = {torque, steeringAngleDeg, curvature, accel, jerk, gas, brake, torqueOutputCan, speed, longControlState}`（op 实际下发值）、`hud = {setSpeed, visualAlert, leadDistanceBars, speedVisible, lanesVisible, leadVisible, leftLaneVisible, rightLaneVisible, leftLaneDepart, rightLaneDepart}`、`cruise = {cancel, resume, override}`、`mads = {enabled, active, available, state}`（carControlSP）

#### `plan` / `loc` / `gps` / `map` 段

- `plan`：`hasLead`/`fcw`、`source`、`aTarget`、`shouldStop`/`allowThrottle`/`allowBrake`、`accelBoost`、`accels[]`/`speeds[]`/`jerks[]`（未来 2.5s 规划序列，20Hz×50 点）、`processingDelay`/`solverExecutionTime`、`desiredCurvature`（lateralManeuverPlan）+ `sp = {source, vTarget, aTarget, sccVision = {state, enabled, active, vTarget, aTarget, currentLateralAccel, maxPredictedLateralAccel}, sccMap = {…}, speedLimit = {valid, speed, dist, source, offset, final, assistState, assistEnabled, assistActive, assistVTarget, assistATarget}, e2eAlerts = {greenLight, leadDepart}}`
  > `speedLimit.speed` 单位 m/s；`sccVision.state` 取值 `disabled/enabled/entering/turning/leaving/overriding`，`assistState` 取值 `disabled/inactive/preActive/pending/adapting/active`
- `loc`：`status`（`uninitialized`/`uncalibrated`/`valid`）、`unixTimestampMillis`、`gpsWeek`/`gpsTimeOfWeek`、`inputsOK`/`posenetOK`/`gpsOK`/`sensorsOK`/`deviceStable`/`excessiveResets`、`timeSinceReset`/`timeToFirstFix`、`posGeodetic = {value: [lat, lon, alt], std, valid}`（lat/lon 度、alt 米）、`velNED`、`accelDevice`、`orientationNED`（同为 Measurement 三元组）
  > 航向可由 `velNED.value` 推导：`bearing = atan2(vE, vN)`
- `gps`：`latitude`/`longitude`/`altitude`、`speed`（m/s）、`bearingDeg`、`horizontalAccuracy`/`verticalAccuracy`/`bearingAccuracyDeg`/`speedAccuracy`、`unixTimestampMillis`、`source`、`vNED`、`hasFix`、`satelliteCount`
- `map`：`speedLimitValid`/`speedLimit`、`speedLimitAheadValid`/`speedLimitAhead`/`speedLimitAheadDistance`、`roadName`（mapd 从在线地图拉取，属 op 独有数据，不违反 §0 回推原则）

#### `model` / `dmodel` / `odom` / `calib` / `params` 段

- `model`：`frameId`/`frameDropPerc`/`modelExecutionTime`/`big`、`confidence`（`red`/`yellow`/`green`）、`engagedProb`、`hardBrakePredicted`、`laneChangeState`/`laneChangeDirection`、`action = {desiredCurvature, desiredAcceleration, shouldStop}`、`laneLineProbs[]`/`laneLineStds[]`、`leads[]`（LeadDataV3 的 t=0 首采样摘要 `{prob, probTime, t, x, y, v, a}`，完整逐点序列体积大不进快照）+ `sp = {laneTurnDirection, leftLaneChangeEdgeBlock, rightLaneChangeEdgeBlock, modelCurvature, blendfulCurvature, blendfulTrust, blendfulGoingAround, blendfulLeftProb, blendfulRightProb, blendfulLaneWidth}`
- `dmodel`：`frameId`/`frameDropPerc`/`modelExecutionTime`/`big`、`action`（同上）、`laneLineMeta = {leftY, rightY, leftProb, rightProb}`、`laneChangeState`/`laneChangeDirection`、`path = {x, y, z}`（三次多项式系数，非逐点序列）
- `odom`：`frameId`、`trans`/`rot`（设备系速度 m/s / 角速度 rad/s）、`transStd`/`rotStd`、`wideFromDeviceEuler`/`wideFromDeviceEulerStd`
- `calib`：`status`（`uncalibrated`/`calibrated`/`invalid`/`recalibrating`）、`calPerc`/`calCycle`/`validBlocks`、`rpyCalib`/`rpyCalibSpread`/`height`/`wideFromDeviceEuler`、`extrinsicMatrix`（3x3 行优先）
- `params`：`latTorque = {valid, useParams, calPerc, latAccelFactorRaw/Filtered, latAccelOffsetRaw/Filtered, frictionCoefficientRaw/Filtered}`、`lateralDelay = {lateralDelay, estimate, estimateStd, validBlocks, status}`、`vehicle = {valid, steerRatio, steerRatioValid, stiffnessFactor, stiffnessFactorValid, angleOffsetDeg, angleOffsetValid, angleOffsetAverageDeg, angleOffsetAverageValid, roll}`

---

## 6. 其余接口

### `GET /sounds/<file.wav>`

sound 事件的配套音效。目录随语言设置切换（`sounds` / `sounds_chs` / `sounds_eng`，
缺失文件回退英文目录），仅接受单个 `.wav` 文件名（路径穿越一律 404）。
浏览器可直接 `<audio src="http://<车机IP>:8083/sounds/engage.wav">`。

### `GET /health`

```json
{ "ok": true, "service": "mate", "uptime": 1234.5, "clients": 2,
  "token_auth": false, "poller_age": 0.1, "onroad": false }
```

不鉴权，供探活脚本使用。`poller_age`=meta 心跳距今秒数（`null`=poller 从未跑起来）；
`ok` 现在等于「HTTP 服务在 + poller 心跳 < 2s」，光看 HTTP 200 不够，探活请断言 `ok`。
`onroad` 透传 params `IsOnroad`，解释 `/state` 数据段为何缺失。

---

## 7. 客户端开发建议

- **初始化流程**：连 WS → 收 `hello`（看 `version`）→ 主动发 `{"type":"snapshot"}`
  （或 HTTP GET `/state`）建立当前状态 → 之后靠事件维护增量、靠 `frame` 维护连续值。
  重连后重复此流程（补发的 `events`/`sound` 已覆盖告警与播报，车速/前车距离等
  连续值由 `frame` 在 ≤0.5s 内自然刷新）。
- **向前兼容**：服务端可能新增 kind 或字段（看 `hello.version`）；客户端对未知
  `type` 与未知字段**一律忽略**，禁止因未知消息报错断连。
- **frame 消费**：状态语义、last-wins——直接以最新帧覆盖 UI 状态，无需排队/去重；
  断线重连期间的帧丢失无害（快照 + 下一帧即恢复）。
- **重连**：指数退避；SSE 浏览器自带。收到 `error` 帧（鉴权失败）禁止盲目重试，
  先引导用户检查 token。
- **对时**：用每段 `ts`（车机墙钟），不要用客户端本地收包时间推算状态时序。
- **枚举容错**：所有枚举为字符串，未来新增值可能显示为 `"unknown"`，客户端要兜底。
- **流量**：边沿事件全订阅约 1–3 条/秒（峰值在按键/告警瞬间）+ frame 2 条/秒
  （约 0.6 KB/s），无需 topic 级节流；`/state` 一次约 4–8 KB。
- **不要公网暴露**：设计为局域网使用；远程访问走 VPN/内网穿透并务必设置 `MateToken`。

## 8. 部署

本仓库已完成注册（`openpilot/system/manager/process_config.py`）：

```python
PythonProcess("mate", "openpilot.mate.server", always_run, restart_if_crash=True),
```

车机部署：把 `openpilot/mate/` 整个目录放到 `/data/openpilot/openpilot/mate/`，
连同修改后的 `process_config.py`，重启 manager 生效。手动调试（前台，Ctrl+C 停）：

```bash
cd /data/openpilot && source launch_env.sh && python -m openpilot.mate
```

验证：`curl http://localhost:8083/health` 返回 `"ok": true`；
`curl -N http://localhost:8083/stream` 能看到 `hello` 后的补发与边沿事件。
卸载：删注册行重启 manager，删 `openpilot/mate/` 目录与
`/data/params/d/MateToken` 即可，无其他残留。

---

## 9. v2 → v3 客户端迁移要点

| 变化 | v2 | v3 | 客户端动作 |
|---|---|---|---|
| `navi` kind | 导航/交通边沿事件 + 快照 navi 段 | **移除** | 删除 navi 消费分支；导航数据（限速/SDI/信号灯/转向）改从客户端自持的上行流计算——其数据源本就是客户端推给 op 的 |
| `frame` kind | ✘ | 2Hz `{car, radar}` 状态帧 | 新增 frame 分支；未升级客户端按 Unknown 忽略即可（无害） |
| `hello.version` | 2 | 3 | 按版本探测能力，未知 type 一律忽略 |
| subscribe 字段 | `kinds`（服务端只认它） | `kinds` | 若客户端误发 `topics` 字段，v2 下会回退全量、v3 语义相同（未知字段忽略）；统一改用 `kinds` |
| WS 鉴权失败 | HTTP 401（握手失败，客户端只见连接错误） | 升级后 `error` 帧 → 关闭 | 增加 `error` 分支，按消息语义提示配对失败，禁止盲目重试 |
| car 段 | — | 新增 `fuelGauge`/`charging` | 全可空模型自然容忍；需要时展示 |
| 快照段 | 含 `navi` 段 | `navi` 段移除，新增 `radar` 段 | 初始化解析去 navi；前车距离初始化改读 `radar` 段 |

### v4 → v5 迁移要点

| 变化 | v4 | v5 | 客户端动作 |
|---|---|---|---|
| `hello.version` | 4 | 5 | 按版本探测能力；未知段一律忽略 |
| `/state` 与 WS `snapshot` | 8 段（car/radar/opstate/events/sound/panda/services/meta） | **新增 18 个纯快照段**（§5.3） | 老客户端无需改动（未知段忽略）；要消费新段（设备看板/规划/定位等）直接读对应段 |
| kind 白名单 | events/opstate/button/sound/frame | 不变 | 无 |
| 推送语义 | 边沿事件 + 2Hz frame | 不变（扩展全部在拉取侧快照） | 无 |

# webui 四亮点合并到 mate 方案（2026-11-28）

> **✅ 已实现（2026-11-28 当日闭环，byd-tici 分支 6 commits
> `f27b3d1e5..238dcedd4`，已推送 origin/byd-tici）**：W1+W2
> `f27b3d1e5`、W3 `bb2eee813`、W4 `7a418c79a`、V1 `5c39c3772`、V2+决策1
> `b5f034271`、收尾 `238dcedd4`。全部决策点已按推荐定案
> （1=A、2=保持6记v6.1、3=做、5=IsLiveStreaming offroad_only=False、6=UI选型A）。
> mate 全量测试 128 passed，ruff 新增文件零错误。唯一剩余：**设备验证
> （offroad）未做**（§8.7）：按门禁，实车/设备实测前必须先过 No-Go 复审。

> 输入：`docs/webui-eval-notes-2026-11-28.md`（mouxancnb/webui v0.4.8 评估）
> 对象：`openpilot/mate/`（协议 v6，server.py 770 行 + poller.py）
> 原则：**不整体移植 webui**（安全姿态差、功能与 mate 大面积重叠），只吸收
> §三的四样工程亮点，并顺手收口 2026-11-15 mate 安全审查遗留的三个问题。
> 门禁不变：No-Go——推送分支 ≠ 放行，实车验证前不刷 mate 实测。

## 0. 总览与优先级（✅ W1-W5/V1/V2 均已实现，设备验证待做）

| 编号 | 项 | 来源 | 优先级 | 一句话 | 状态 |
|---|---|---|---|---|---|
| W1 | 集中式 offroad 守卫 + **修审查问题 ②（confirm TOCTOU）** | webui `offroad_guard.py` 形状 | **P0** | approve 执行前复查 offroad，行车中批准也拦下 | ✅ `f27b3d1e5` |
| W2 | **修审查问题 ③**（hub.onroad() 不查 poller 心跳） | 自建（webui 未覆盖心跳语义） | **P0** | poller 死了 onroad() 返回 None → 503 拒写，不许拿陈旧 False 放行 | ✅ `f27b3d1e5` |
| W3 | param_changes 哈希链审计 | webui `param_changes_service.py` | **P1** | "谁什么时候改了什么"，断链可定位，调参排障刚需 | ✅ `bb2eee813` |
| W4 | param 白名单 schema 化（panel catalog 思想） | webui `panel_catalog` | **P1** | 白名单条目变数据表，加键不改门控代码 | ✅ `7a418c79a` |
| W5 | 审查问题 ①（token 未配置即放行）收口 | mate 自身 | **P1·决策点** | 见 §5 决策点 1（定案 A） | ✅ `b5f034271` |
| W6 | PC mock 开发回路 | webui `dev/run_pc.py` + SIM | **P2·可选** | mate poller 依赖 cereal 订阅，PC mock 成本高于 webui，见 §4 | ⏸ 后置（未做） |
| V1 | WebRTC 视频流代理 | webui `webrtc_api.py`（67 行） | **P1·决策点** | webui 并没有实现视频——它只代理本仓自带的 webrtcd；mate 同样代理即可，见 §7.1 | ✅ `5c39c3772` |
| V2 | **mate 自建 Web UI**（用户定案：做 UI + 选型 A 无构建链，2026-11-28） | 自建（M0-M2 分期） | **P1** | mate 同端口托管 vanilla 单页：状态/调参/命令/审计/视频五页，**不 fork webui 前端**，见 §7.2 | ✅ `b5f034271`（M0/M1/M2 一次到位，~750 行） |

明确**不移植**清单见 §6。

---

## 1. W1：集中守卫 + 修 confirm TOCTOU（P0）——✅ 已实现 `f27b3d1e5`

### 1.1 webui 答案里对的和错的

webui `offroad_guard.py`（~50 行）做对了三件事：**集中共享**（`require_offroad()`/`require_disengaged()` 一处定义）、**执行时逐请求复查**（不是入口查一次就完）、**结构化 error 返回**。

但它有个致命缺陷，明确**不采纳**：

```python
except Exception:
    pass
return False   # ← cereal 读失败 → 判定 offroad → 放行
```

读不到状态 = 默认可执行，正是 mate 审查 ② 的同类 fail-open。mate 的既有口径
（`_param_check_offroad` / `_command_check_offroad`）是「未知即拒」（503），必须保持。

### 1.2 mate 现状与 bug 定位

`server.py` 现有两条门控路径，且**分散**：

- `_param_check_offroad(name)`（server.py:183）— param 写前门控，口径正确；
- `_command_check_offroad(name)`（server.py:332）— **只在 `POST /command` 时查一次**（server.py:429）；
- `command_confirm_handler`（server.py:442）approve 分支（server.py:461-466）**直接执行 executor，不复查 offroad**。

**TOCTOU 链**：offroad 发起 `/command`（过门控，进 pending）→ 车辆 onroad → 60s 内客户端 approve → executor 在行车中执行 `reboot_device`（DoReboot）/`clear_logs`。confirm 窗口 60 秒，这个窗口不窄。

### 1.3 方案

1. 新增集中守卫（落在 server.py，不另立模块——mate 单文件结构，webui 那种
   56 模块 bridge 是另一种规模）：

```python
def _guard_offroad(*, what: str) -> web.Response | None:
  """集中 offroad 门控：onroad 拒（409），状态未知拒（503）。
  param 写与命令执行共用；调用方传 what 区分报错文案。"""
```

   `_param_check_offroad` / `_command_check_offroad` 收编为对它的调用，消灭两份
   重复的 `hub.onroad()` 三态判断。

2. **修 ② 的关键一行**：`command_confirm_handler` approve 分支，在 `_pop_confirm`
   成功、`_resolve_confirm(approved)` 之前插入复查：

```python
  offroad_reject = _guard_offroad(what=command)
  if offroad_reject is not None:
    _resolve_confirm(confirm_id, "blocked_offroad")
    return web.json_response({"ok": False, "error": "..."}, status=409)
```

   语义细节：此时 confirm 已 pop、不再可执行，broadcast `blocked_offroad` 终态
   让客户端关弹窗；客户端想再执行需重新发起 `/command`（重新过门控）。

3. 残余风险如实记录：复查到 executor 之间仍有亚毫秒级窗口（asyncio 单线程 +
   `to_thread` 执行），且 `DoReboot` 由 manager 轮询消费、消费时点更晚。这个
   残余窗口与"命令本身 offroad 语义"（重启/清日志本来就该停车做）叠加后
   风险可接受，不再加锁追杀。

### 1.4 测试

`tests/test_server_commands.py` 补三个用例：
- pending 期间模拟 onroad（hub snapshot 注入 meta.onroad=True）→ approve 返回 409、broadcast `blocked_offroad`、executor 未调用（mock executor 计数）；
- onroad 状态未知（meta 缺失）→ approve 返回 503；
- 回归：offroad 下 approve 全链路照常执行。

---

## 2. W2：hub.onroad() 查 poller 心跳（P0，修审查 ③）——✅ 已实现 `f27b3d1e5`

### 2.1 现状

`Hub.onroad()`（server.py:570）只读 meta 快照的 `onroad` 字段，**不检查快照新鲜度**。poller 线程死后，meta 残留最后一次写入值——若停机时是 offroad（False），此后所有 offroad_only 写门控都拿到 False 直接放行，直到 mate 进程重启。`/health` 有 `poller_age`（server.py:726），但门控不看它。

webui 的 state_hub 也没有这个语义（评估 §2.5 结论），属 mate 自建修复。

### 2.2 方案

`Hub.onroad()` 内先查 meta 新鲜度，复用既有 `POLLER_STALE_S = 2.0`：

```python
def onroad(self) -> bool | None:
  with self._snapshot_lock:
    meta = self.snapshot.get("meta")
  if meta is None:
    return None
  if wallclock() - meta["ts"] > POLLER_STALE_S:
    return None          # poller 心跳死了：状态未知，调用方按 503 拒
  return bool(meta["data"].get("onroad"))
```

poller 每轮（≤100ms）刷 meta（poller.py:945），2s 阈值余量充足。注意保持
`/health` 的 `onroad` 字段口径一致（它直接调 `hub.onroad()`，自动跟随，health
的 ok 判定本就有 poller_age，无回归）。

### 2.3 测试

`tests/test_server_hub.py` 补：meta 写入后人为把 `ts` 拨旧 3s → `onroad()` 返回 None；`_param_check_offroad` 对 offroad_only 键返回 503 响应。

---

## 3. W3：param_changes 哈希链审计（P1）——✅ 已实现 `bb2eee813`

### 3.1 为什么要

参数可从 mate API、车机 UI、拨轮按钮、备份恢复多条路写入；出问题时无法回答
"谁、什么时候、从什么改成什么"。webui 的实现（port 自 carrot cp）质量好：
append-only JSONL、每条记录携带前一条 hash、`verify` 从头重走报第一处断链、
环形裁剪（1000 条）时**重建链**保证裁剪后仍可验证、日志失败不拖垮主写入。

### 3.2 移植裁剪（按 mate 规模）

新增 `openpilot/mate/param_changes.py`（独立模块，预计 ~120 行），从 webui 版本裁剪：

**保留**：`record_hash`（字段：ts/name/prev/next/source/prev_hash → hash）、
`append_param_change`（永不抛异常）、`read_param_changes`（name/source 过滤 +
limit，新在前）、`verify_param_changes`、`_trim_locked`（裁剪重链）、
genesis hash `"0"*64`。

**裁掉**：fingerprint baseline 全套（`read/write_fingerprint_baseline`、
`param_fingerprint`、`get_settings_fingerprint`、`count_changes_since`）——
mate 白名单仅 21 键，全量摘要对比价值低；将来键多了再补。

**改**：
- 状态目录：`/data/mate/param_changes.jsonl`（对齐 `GEOMETRY_PATH_DEFAULT`
  的 `/data/mate/` 惯例，环境变量 `MATE_PARAM_CHANGES_PATH` 可覆盖，测试注入用）；
- `source` 枚举对齐 mate 通道：`{"param_api", "command", "device", "unknown"}`；
- 时间戳用 `wallclock()` 语义（int 墙钟秒），与 mate 既有 ts 口径一致；
- `time.time` → 仓库 lint 禁用，一律换 `wallclock()`。

### 3.3 接线点

| 写入路径 | 接法 |
|---|---|
| `param_set_handler`（server.py:266） | put 成功后 `append_param_change(name, prev, echo, source="param_api", engaged=hub.onroad())`；prev 在 put 前读一次（读失败记 None） |
| `command_confirm_handler` / `command_handler` | 命令类参数写入（`DoReboot` 等）也记账，`source="command"` |
| 设备侧漂移（车机 UI / 按钮改的） | 见下，`source="device"` |

**设备漂移观察**（webui `observe_param_values` 的对应物）：poller 线程已有
`Params()` 实例与主循环（poller.py:929），增加一个 **2s 节流**的旁路任务：
读一遍 `PARAMS_WHITELIST` 全部键（21 个文件读，2s 一次，offroad/停车时代价
可忽略），与内存缓存比对，漂移即 `append_param_change(..., source="device")`，
并回灌缓存。**不做** 100ms 全量轮询——每轮 21 次 get 是 poller 主循环的
实打实开销，节流到 2s 足够排障用途。已知值缓存的初始化：首次读到即建基线
（不产生假记录），与 webui 语义一致。

### 3.4 新端点（HTTP，additive）

```
GET /param_changes?limit=&name=&source=   审计记录（新在前）
GET /param_changes/verify                  哈希链校验（断链报 broken_at + reason）
```

两个端点走 `check_auth`（读类口径）。响应形状沿用 `{"ok": ...}` 惯例。

### 3.5 测试

新增 `tests/test_param_changes.py`：
- 链完整性：连续 append → verify ok；
- 篡改检测：改中间一条的 next 值 → verify 报 broken_at；
- 截断检测：删尾行/半行 → verify 定位；
- 裁剪重链：超过 MAX 后 verify 仍 ok；
- 漂移观察：注入缓存 + 改值 → 产出 source=device 记录；
- 永不抛异常：目录不可写时 append 返回 None、param_set 正常成功。

---

## 4. W4：白名单 schema 化（P1，panel catalog 思想）——✅ 已实现 `7a418c79a`

> 实施记录：采用 dataclass ParamSpec（group 分组 navi/slc/mads/long/misc）；
> `IsLiveStreaming` 入白名单（offroad_only=False）；新增 `GET /params/schema`；
> 漂移观察未放 poller 而是做成 server.py 内 asyncio 2s 节流旁路任务
> （与 poller 线程解耦，行为同 §3.3 设计）。

### 4.1 目标形态

webui 的 panel_catalog 是 16 面板全 schema 化（widget/约束/分组）；mate 不做
面板层，只把 `PARAMS_WHITELIST`（server.py:147，21 键）演进为**数据驱动**：
现结构 `(kind, lo, hi, offroad_only)` 已是半个 schema，补两列描述性字段即可，
门控/coerce/审计逻辑全部只读这张表——**加键 = 加一行数据，零代码改动**。

```python
PARAMS_WHITELIST: dict[str, ParamSpec] = {
  # key: {kind, lo, hi, offroad_only, group, desc}
  "AutoLaneChangeTimer": ParamSpec("int", -1, 5, True, group="alc", desc="0.5-3s 变道延时，-1=Off"),
  ...
}
```

（用 `dataclass` 或仍用 tuple，实施时定；倾向 dataclass——21 键规模下可读性
优先，字段访问不再靠 `[3]` 下标，消灭 `_param_check_offroad` 里的魔法数字。）

`group` 按现有注释语义划分：`navi` / `slc` / `mads` / `long` / `misc`。
字段立即有消费方：审计查询按 group 过滤、**V2 调参页按 group 分组渲染**
（§7.2.1）——不是预留字段，M1 依赖它。

### 4.2 不做的事

- 不做 webui 的 `key+"Lock"` 备份裁剪、catalog 备份恢复——mate 无备份作用域概念；
- 不做 GUI widget 类型——mate 是 API 服务，无前端面板。

### 4.3 测试

现有 `test_server_params.py` 全量回归即可（schema 化不改行为，只改结构）；
补一条「白名单完整性」元测试：每个键四要素齐全、值域 lo≤hi。

---

## 5. W5 + 待决策点（✅ 全部已按推荐定案并落地）

1. **① token fail-open 是否收口**。现状：`MateToken` 未配置时
   `require_sensitive_auth` 直接放行（server.py:322），且 mate 绑
   `0.0.0.0:8083`（server.py:765）。攻击面比 webui 小（无系统级破坏动作、
   参数写有 offroad 门控），但 `/command` 在无 token 局域网内可触发
   reboot_device。可选：
   - **A（✅ 已选定并实现）**：保持"未配置即放行"（车内私有网络定位），但把
     `bind host` 收窄为可配（`MATE_HOST`，默认仍 0.0.0.0）+ `/command`
     响应头加 `X-Mate-Auth: none|token` 提示客户端当前裸奔状态；文档明示。
   - B：改为 fail-closed——token 未配置时 `/command` 系列直接 503。
     车内自用场景每次都要先配 token，摩擦大。（未采纳）
   - C：维持现状不动。（未采纳）
2. **协议版本**：✅ **保持 6、记 v6.1 additive**（server.py 文档串与 API.md §11
   已同步；carrotmate 客户端无需跟随）。
3. **设备漂移观察**：✅ **本期做了**（server.py asyncio 2s 节流旁路，
   source=device）。
4. **W6 PC mock**：⏸ **后置未做**（`MATE_DEV_PC` 占位也未留——实施时确认
   无消费方，纯占位反而误导，将来真做 PC 回路时再加）。
5. **V1 视频流代理**：✅ **做了**；`IsLiveStreaming` 门控取 **A：offroad_only=
   False 任意时刻可写**。
6. ~~V2 技术选型~~ **已定案：A，vanilla JS 无构建链**（2026-11-28 用户拍板）。
   `openpilot/mate/web/` 手写单页（index.html + app.js + style.css，实际
   ~750 行，M0-M2 一次到位），aiohttp 直接托管，设备零构建、随仓库走、
   PC 上改完即验。将来功能长胖再评估 Vite+React+AntD 迁移（注意是重写不是渐进）。

---

## 6. 明确不移植清单

| webui 功能 | 不移植理由 |
|---|---|
| webui 本体 / 16 面板 / WebRTC / Wi-Fi / SSH key 管理 | 功能与 mate+原生 UI 重叠；安全姿态差（评估 §2），上车=放大攻击面一个数量级 |
| `reset_all_params` | `p.clear_all()` 无守卫即抹掉全部配置，属危险反模式 |
| `ssh_api`（写 GithubSshKeys 参数） | 跨域写公钥 → SSH 接管链，直接摘除 |
| git 自更新 / agnos_install 裸奔入口 | 系统级动作必须走 offroad+confirm 闭环（mate 语义），webui 版无守卫 |
| CORS 反射任意 Origin | mate 是 API 服务无浏览器前端；将来若加 web 前端，一律白名单 |
| `offroad_guard` 的 `except: return False` fail-open | 与 mate「未知即拒」口径相反（§1.1） |
| fingerprint baseline 全套 | 21 键规模价值低（§3.2），键多了再补 |

---

## 7. V 线：视频流（V1）与前端（V2）

### 7.1 V1 视频流：mate 做 webrtcd 代理（P1，成本 ~100 行）——✅ 已实现 `5c39c3772`

**事实核对**：webui 没有自己的视频实现。`server/bridge/webrtc_api.py` 全部
67 行，只是把浏览器的 SDP offer 转发到 `127.0.0.1:5001` 的 webrtcd
（`system.webrtc.helpers.post_stream_request`）。真正的链路是：

```
camerad（采集，onroad 本就运行）
  ├─ encoderd          → roadEncodeData（HEVC，行车日志主路）  ← 驾驶关键，不碰
  └─ stream_encoderd   → livestream*EncodeData（H.264）        ← 仅直播旁路
                            ↓
                        webrtcd（:5001，WebRTC 信令+推流）
                            ↓
                     浏览器 WebRTC 解码
```

**本仓基建已全齐**（process_config.py）：

- `stream_encoderd`（:185，NativeProcess `./encoderd --stream`）
- `webrtcd`（:234，PythonProcess，监听 localhost:5001）
- 两者都由 `IsLiveStreaming` param 门控（:128 `livestream()`），offroad/notcar
  也可跑——不需要动 manager 任何东西。

所以 mate 的 V1 只是**把 webui 那层代理抄过来**（它本来就写得干净）：

```
GET  /webrtc/schema                 透传 webrtcd /schema（webrtcd 是否就绪）
POST /webrtc/offer                  {"sdp": "...", "camera": "road|wideRoad|driver"}
                                    → helpers.post_stream_request → 回 SDP answer
```

实现要点：

1. **鉴权**：走 `require_sensitive_auth`（对齐 /command 口径）。行车画面
   （尤其 driver 摄像头）是隐私敏感面，比参数写更该锁；决策点 1 若选
   fail-closed，V1 必须同口径。
2. **camera 归一**：照抄 webui `_normalize_camera` 的别名表（客户端报
   topic 名也认，非法值回落 road，不信任输入）。
3. **不代理媒体面**：WebRTC 媒体走 ICE/UDP 点对点（浏览器 ↔ webrtcd），
   mate 只代理信令；webrtcd 只监听 localhost，信令经 mate 转发，SDP 里的
   ICE 候选地址仍指向设备局域网 IP——局域网内可达，无需打洞。
4. **webui 的优化文档只取红线**（`docs/WEBRTC_OPTIMIZATION.md`）：P0 红线
   = 禁止为直播改 modeld/controlsd/plannerd/pandad、encoderd 主路、
   camerad 采集参数。mate 代理层天然不碰这些；stream_encoderd 按需启停
   由 `IsLiveStreaming` 承担，mate 不做码率/优先级调优。
5. **不做** stream_health_api（webui 的直播健康分）——一期用
   `GET /webrtc/schema` 的可达性 + 客户端自身卡顿感知即可。
6. `IsLiveStreaming` 加入 `PARAMS_WHITELIST`（取值见决策点 5），这样
   开关直播走既有 param API + 审计（W3 会自动记账）+ offroad 门控。

### 7.2 V2：mate 自建 Web UI（2026-11-28 定案，✅ 已实现 `b5f034271`）

**不 fork webui 前端**（20.6k 行 vanilla JS / 1MB static，维护黑洞），也不
复刻 BIG UI——mate UI 是自己的轻量单页，**数据面就是 mate 的 API 面**，
两者同仓同版本演进，不存在"前端落后于协议"的问题。

#### 7.2.1 页面规划（五个页面，与 API 一一对应）

| 页面 | 数据源 | 内容 |
|---|---|---|
| 状态（M0） | `/health` + `/state` + `/ws` | onroad/poller 心跳徽章、clients/token 状态；car/radar frame 2Hz 连续值表；events/opstate/sound 实时流（WS 订阅，断线重连）；19 快照段折叠查看 |
| 调参（M1） | `/params` + `/param_set` | **W4 ParamSpec 驱动渲染**：按 group 分组（navi/slc/mads/long/misc），bool→开关、int/float→带值域的输入，desc 做行内说明；offroad_only 键在 onroad（或未知）时禁用并标注原因（409/503 文案透传）；写后读回对账（`value` echo） |
| 命令（M1） | `/command` + `/command/confirm` + WS confirm | 3 命令卡片；confirm 弹窗带倒计时（`timeout_s`），approve/deny 后按广播终态关窗；`blocked_offroad` 终态显示"行车中已拦截" |
| 审计（M1） | `/param_changes` + `/param_changes/verify` | 变更记录表（时间/key/prev→next/source 徽章），顶部 verify 徽章（ok 绿 / broken_at 红）；name/source 过滤 |
| 视频（M2） | `/webrtc/schema` + `/webrtc/offer` | road/wideRoad/driver 三路切换 `<video>`；schema 不可达显示"直播未开启"（引导开 `IsLiveStreaming`） |

不做：驾驶屏复刻、WebGL 车道线、i18n 多语言（中文单语）、onboarding——
那是 webui/BIG UI 的事。

#### 7.2.2 托管与鉴权（同端口，消灭 CORS 问题）

- **同端口托管**：aiohttp `app.router.add_static('/ui/', UI_DIR)` +
  `GET /ui` 302 → `/ui/index.html`。UI 与 API 同源 → **fetch 全部 same-origin，
  从结构上消灭 webui 那个 CORS 反射漏洞**（API 保持无 CORS 头 = 跨域一律拒）。
- **新增鉴权中间件**取代散落的 `check_auth` 调用：`/ui/*`、`/webrtc/*`、
  `/param_changes*` 统一走中间件（`/health` 仍豁免）；WS 维持既有
  `?token=` error 帧语义不动。
- **UI 侧 token**：URL 带 `?token=` 进来（与 /sounds 客户端约定一致）→
  存 sessionStorage 后从地址栏抹掉 → 后续 fetch 带 Bearer 头、WS URL 带
  `?token=`（浏览器 WS 无法自定义 header，query 是唯一通道）。
  MateToken 未配置时无感直进（对齐既有 fail-open 口径，见决策点 1）。
- 资产目录 `openpilot/mate/web/`（index.html / app.js / style.css），
  `main()` 里 `UI_DIR` 不存在时跳过静态路由——纯 API 模式零开销。

#### 7.2.3 技术选型（已定案：A 无构建链）

- **A（已选定）vanilla JS 无构建链**：~1200 行手写单页（fetch + WebSocket +
  原生 WebRTC API 全部够用），设备零构建、改完 rsync 即生效；
- 将来若 UI 长胖（图表、多设备管理）再评估 Vite+React+AntD 迁移——
  届时是重写，UI 代码重来，这是选 A 已接受的成本。

#### 7.2.4 与 webui 前端的关系

只借**设计事实**，不借代码：panel_catalog 的"数据驱动面板"思想已在 W4 落地
（调参页由 ParamSpec 渲染）；webui 的 WS 重连/退避策略可参考其 `ws.js`
（270 行）的节奏参数，不抄结构。

### 7.3 补进不移植清单

| webui 功能 | 不移植理由 |
|---|---|
| 前端 BIG UI 复刻（20.6k 行 JS / WebGL） | fork 维护黑洞；mate UI 自建轻量单页（§7.2），不复刻 |
| stream_health_api 直播健康分 | 一期收益低，schema 可达性够用（§7.1.5） |

---

## 8. 实施顺序与验证（1-6 ✅ 已完成；7 设备验证待做）

1. ✅ **W1+W2（P0，一批）**：guard 集中 + confirm 复查 + onroad() 心跳检查，
   同批补测试（§1.4 / §2.3）。这两项是纯 server.py 改动，风险最小、
   闭合审查遗留。——`f27b3d1e5`
2. ✅ **W3 审计**：新模块 + 端点 + 漂移观察旁路（决策点 3=做）+ 测试。
   ——`bb2eee813`
3. ✅ **W4 schema 化**：重构 PARAMS_WHITELIST，全量回归。——`7a418c79a`
4. ✅ **V1 视频流代理**（决策点 5=做）：`/webrtc/schema` + `/webrtc/offer`
   + `IsLiveStreaming` 白名单条目（mock 信令测试，不依赖真实 webrtcd）。
   ——`5c39c3772`。设备 offroad 验证待做（§8.7）。
5. ✅ **V2 mate UI**（技术选型 A 无构建链，M0/M1/M2 一次到位）：
   ——`b5f034271`
   - ✅ M0 骨架：`mate/web/` 资产 + `/ui` 静态托管 + auth_middleware 收口 +
     状态页（/health 徽章 + /state 快照 + WS 事件流，含断线重连）；
   - ✅ M1 调参/命令/审计：ParamSpec 驱动调参页（onroad 锁定态）+ 命令
     confirm 弹窗闭环（含 blocked_offroad 终态展示）+ 审计页（verify 徽章）；
   - ✅ M2 视频：WebRTC 三路切换页，依赖 V1。
6. ✅ **文档同步**：`mate/API.md` §11 已增补全部新端点与新语义；
   server.py 文档串已记 v6.1。——`238dcedd4`
7. ⏳ **设备验证（offroad，唯一剩余项）**：SSH 设备起 mate → 浏览器开 `/ui` →
   状态页数据活 → 调参页写一个键 → 审计页出现记录且 verify 绿 → 车机 UI 改
   一个白名单键 → 2s 内审计页出现 device 漂移记录 → （V1）视频页放流
   → 关流。onroad 拒写路径不实车测（门禁），用 hub snapshot 注入模拟；
   confirm blocked_offroad 弹窗态同法模拟验证。

实施结果：6 commits `f27b3d1e5..238dcedd4`（byd-tici，已推送
origin/byd-tici），mate 全量测试 **128 passed**，新增文件 ruff 零错误。
实际改动：server.py（guard/审计接线/中间件/V1 代理/静态托管）、新增
param_changes.py、漂移观察为 server.py 内 asyncio 任务（poller.py 未动）、
mate/web/ 三件套 ~750 行、tests +4 文件、API.md §11。

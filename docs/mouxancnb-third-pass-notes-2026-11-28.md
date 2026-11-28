# mouxancnb/openpilot 第三轮挖掘（2026-11-28）

> 前两轮覆盖：AGNOS、virtual sensord、字体、locationd、tc-v61、radard gc、modem、盲区音频等。
> 本轮补挖更早历史（11 月中旬及以前）+ 专项领域（athena/时钟/硬体守护/CI/AI agent）。

## 摘要表

| 提交 | 内容 | 本地判定 |
|---|---|---|
| `0b93bd560` | 时钟同步改漂移驱动（HTTP Date 双向 >60s 校正） | ✅ P0，本地缺口更严重 |
| `11153aeeb` | swaglog 遥测洪水饿死相机（athena onroad 暂停上传+限速排空） | ✅ P1，athena 部分适用 |
| `76f9bdef3` | hardwared touch_thread open 失败→重启循环→永远进不了 onroad | ✅ P1，本地同款脆弱模式 |
| `c0208933b` 等 | 设备端内置 AI agent（opencode web + MCP 三重 fail-closed 门） | 🔮 安全设计范本，值得研究 |
| `0a1ee9652` | CI 子模块"静默空检出"检测与自愈 | ⚠️ P2，按需 |
| `8fa56ad05` | sentry 告警经 PWA web-push / Discord / ntfy 推送 | 🔮 方向 |
| `db13a0965` | GPS 融合 phone/navi fix（隧道存活）+ CarrotException 死消费者修复 | 🔮 方向 + 排错清单 |
| `b96a49e8c` | webrtcd 三 bug（str "1" 不满足 BOOL param 类型检查等） | ⚠️ 排错清单 |

---

## 1. 时钟同步漂移驱动（`0b93bd560`）— P0，本地缺口比源库更大

**源库事故**：RTC 停在 2026-07-28，通过"pre-2025-01-01"纪元守卫 → HTTP Date 同步从不运行 → 之后所有 CNB release 下载 TLS 握手报 **"certificate is not yet valid"** 全部失败 → 刷机工具空转数小时（设备 SSH 可达、只有时钟是错的）。

**本地核对**：
- 设备 **无 COINCELL 充电**（AGNOS 文档实测 config 全 =n）→ 全断电后 RTC 大概率漂移，正是源库同款故障前提；
- 本地 `timed.py` 仅 GPS 校时（要求 GPS fix，车库内无效）+ 依赖 AGNOS NTP；
- **本地 `launch_chffrplus.sh` 完全没有任何时钟同步**（grep 无一处 date -s/clock/ntp）；
- 你的 release 产物从 CNB 下载 → TLS 证书时间校验失败楔死 updater 的模式完全适用。

**落地**：`sync_clock_if_drifted()` ~25 行，HTTP(80) Date 头、双向 >60s 漂移即校正，http://www.baidu.com / qq.com 国内可达源已在函数里。建议放 launch 早期、任何 TLS 下载之前。

## 2. swaglog 遥测洪水饿死相机（`11153aeeb`）— P1（athena 部分适用）

**源库实测**：radarcan/radard/ui/signalyolod 的 RuntimeDiagnostics 1s 间隔 + signalYoloShadow 逐帧 swaglog event ≈ 200-300KB/min → /data/log 积压 2500 文件 ~460MB → **每次 App 重连 athena 排空整个积压**（负载 15+，camerad 卡在 cam_sync_wait D 态）→ 行车中相机丢帧、模型滞后、camera-fault 告警。三处修复：诊断间隔 1s→10s、shadow event 限 10s 一次、**athena log_handler onroad 暂停上传 + offroad 限速 1 文件/3s**。

**本地核对**：
- 洪水源（runtime_diagnostics.py / signal_yolo_shadow.py）本地**不存在**，这半边无需动；
- 但本地 `athenad.py log_handler` **逐循环上传、无 onroad 暂停、无限速**——任何原因造成的大积压（含你手工 scp 后的混乱状态）都会在重连时重演同款饿死相机。

**落地**：athena 部分 ~30 行独立可移植。另注意：你的 locationd 鲁棒性主题（消灭中途误脱开）又多了一个"因"层面的修复项——radard gc（第二轮）+ 本条都是"内存/CPU 压力 → 相机/模型卡顿 → 误告警"链的提前拦截。

## 3. hardwared touch_thread 重试（`76f9bdef3`）— P1

**源库事故**：无屏 comma three 触摸节点不枚举 → `open()` 抛 IsADirectoryError 线程立即死 → main() 退出 → manager 每 ~2s 重启 hardwared → 每次重启重置 hardware_thread 的 sm.frame → not_onroad_cycle 永远 false → **点火后永远进不了 onroad**。

**本地核对**：`hardwared.py:151` 本地同款 `with open(TOUCH_EVENT_DEVICE, "rb")` 脆弱模式。你有屏，触发概率低，但克隆屏 TSP 生态本就不稳（触摸直读 evdev、此前就修过时间戳），节点延迟出现/悬空 symlink 的防御值 15 行：open 失败重试到 end_event 为止。

## 4. 设备端内置 AI agent（`c0208933b`/`6821cad94`/`515362744`/`971b23341`）— 🔮 安全设计范本

opencode web（端口 5095，**仅 offroad**）跑在设备上，配 stdlib-only 的 stdio MCP server（comma_mcp.py），让 AI agent 能读写参数、查状态。**安全设计值得逐条研究**（你在做 Web 漏洞研究，这套是对口的）：
- **三重独立 fail-closed 门**：每个写工具调用都重查 IsOffroad（不信任入口检查）、读/写参数双白名单、**控制类参数黑名单压过白名单**（test 锁定）；
- **不给 shell 工具**：生成配置里 opencode 自己的 bash 权限显式 deny、edit ask、webfetch deny；
- 版本钉死 v2.3.0；CPU 亲和绑离 realtime 核；空闲 watchdog 写 AgentSuspended 再干净退出防 manager 复活。

对你两重价值：①将来如果想在设备上跑 AI 调试助手，这套门禁是现成蓝本；②它正是"openpilot + AI 工具链"的实做样本，与你 Claude Code/Codex 评估线可以互相参照。

## 5. 其余项

- **CI 子模块静默空检出**（`0a1ee9652`）：shallow clone 下 `git submodule update` 的 checkout 可能**静默失败**——状态无 -/+ 前缀、HEAD 已对，但 worktree 文件根本没落地（panda 210 个文件）。本地 release.yaml 用 `submodules: true`（checkout action），目前不吃这个坑；一旦改为手动 submodule update 流程，把它的探测/自愈（按 pinned sha diff worktree 文件数）带上。
- **sentry 告警推送**（`8fa56ad05`）：offroad 告警经 carrot PWA web-push（VAPID）+ 可选 Discord/ntfy webhook，去重/节流语义完整（warning 沿触发、alarm 60s 窗、24h 过期）。依赖 carrot PWA 管线，本地无；方向项。
- **GPS 融合 + CarrotException**（`db13a0965`）：phone/navi fix 新鲜时融合导航定位（隧道/峡谷存活），依赖 carrot gps_sources。**排错清单价值**：CarrotException 消费者曾整个死掉——`Params.getBool 只匹配 "1"` 而 writer 写的是 reason 字符串，即"参数类型契约两端不一致→静默失效"模式，审查本地 params 消费者时值得带一眼。
- **webrtcd 三 bug**（`b96a49e8c`）：其一为"BOOL param 写 str '1' 过不了 python2cpp 类型检查"——同款陷阱适用于任何写 BOOL param 的代码；其二三为 aiortc/teleoprtc API 细节，本地无 webrtc 场景，不适用。
- **panda 自动检测外部 Red Panda**（`393da556b`）：你的 panda 是焊死 F4 dongle，不适用。

## 三轮总汇（动手清单合并）

| 序 | 项 | 量级 | 来源轮次 |
|---|---|---|---|
| 1 | radard gc.collect | 10 行 | 第二轮 |
| 2 | modem 重拨退避 | 25 行 | 第二轮 |
| 3 | 时钟漂移同步 | 25 行 | 第三轮 |
| 4 | athena onroad 暂停上传+限速 | 30 行 | 第三轮 |
| 5 | locationd ④+①（manager join 限时 + 低速 gyro 豁免） | ~60 行 | 第一轮 |
| 6 | blindspot_alert 盲区音频 | ~110 行 | 第二轮 |
| 7 | navi generation re-arm | ~15 行 | 第一轮 |
| 8 | hardwared touch 重试 | 15 行 | 第三轮 |
| 9 | AGNOS 一致性门 + 字体两小修 | 半天 | 第一轮 |

1/2/3/4/8 全是"行车中断链"或"刷机楔死链"的防御项，彼此独立、可逐个小步提交；6 是唯一新功能（默认关）；7/9 是既有线收尾。

# mouxancnb/webui 子模块评估（2026-11-28）

> 评估对象：`cnb.cool/mouxancnb/webui`（openpilot 内 `webui` 子模块，浅克隆 /tmp/webui-eval 全量核对）
> 版本 v0.4.8 / GUI v78。定位：**浏览器复刻 sunnypilot BIG UI**——16 个设置面板 + 行车 HUD + WebRTC 相机。

## 一、规模与架构

| 项 | 数据 |
|---|---|
| 服务端 | aiohttp，`server/bridge/` 56 个模块 ~18k 行，99 条路由 |
| 前端 | ~20k 行 JS（无框架，WebGL 车道线 + DOM HUD） |
| 进程 | `python -m webui.webuid`，默认 **0.0.0.0:5080**，HTTP 302→HTTPS（自签证书，为 WebCodecs） |
| 功能 | 16 面板 params 读写、重启/关机/卸载/AGNOS 安装、Wi-Fi 管理、SSH key 管理、模型管理器、IMU 标定、WebRTC 前路/驾驶员相机、git 自更新 |
| 质量 | 9 个 bridge 测试 + 4 个顶层测试；PC mock（`WEBUI_DEV_PC=1` + SIM 模拟面板）可全程脱离设备开发 |

架构干净：`cereal/Params → state_hub → /api/opui/state` 单一状态端点；面板全由 `panel_catalog` 数据驱动；`param_constraints.py` 在每次写后**镜像原生 UI 的约束清理**（如 ADB 与 Jetlink 抢 USB 口的互斥）。

## 二、安全评估（重点，对照 mate v6）

**结论先行：安全姿态为"可信局域网专用"，实际达到"任意浏览器网页可全权接管"。功能越全，攻击面越大。**

### 2.1 致命组合（三条叠加）

1. **零鉴权**：全库 grep 无 token/password/session/Bearer——app_factory、deps 层没有任何鉴权中间件。
2. **默认 0.0.0.0:5080**：`webuid.py:71` 默认绑全网卡（可用 WEBUI_HOST 收窄但默认不收）。
3. **CORS 反射任意 Origin**（`app_factory.py:35`）：`Access-Control-Allow-Origin = <请求 Origin>` 无白名单——**任何你浏览器打开的网页，其 JS 可以直接跨域读写设备全部 API**，连 DNS rebinding 都不需要。这就是"驾车时打开一个恶意网页 = 参数被任意改写"的完整链。

### 2.2 无守卫的系统动作（`system_api.py run_action` 实读）

- `reboot` / `shutdown` / `uninstall` / `updater_install` / `agnos_install`：**无鉴权、无 offroad 检查、无确认**，直接 `put_bool(DoReboot/DoShutdown/DoUninstall)`。
- **`reset_all_params`：无守卫 `p.clear_all()` + 立即重启**——一个跨域请求抹掉全部配置（SSH key、标定、标定参数全没）。
- 全部 18 个动作中只有 `reset_calibration` 有 `require_offroad() + require_disengaged()`。

### 2.3 offroad 守卫覆盖率与 fail-open

- `require_offroad` 仅出现在 **5/56** 个 bridge 模块；params 写入、SSH key 写入、Wi-Fi 配置等全部无守卫（行车中可写）。
- 多处守卫包在 `try/except Exception: pass` 里（如 `restore_params` 的 offroad 检查）——**import 一断守卫静默消失**，fail-open 模式。

### 2.4 SSH 接管链

`ssh_api.py` 可写 `GithubUsername`/`GithubSshKeys` 参数（拉任意 GitHub 用户公钥写入）。设备 SSHD 若消费该参数（上游 comma 机制如此），则**跨域请求 → 写自己的公钥 → SSH 接管设备**。安装脚本里无任何防火墙/iptables 加固。

### 2.5 对照 mate v6 的启示

| mate v6 问题（2026-11-15 审查） | webui 的答卷 |
|---|---|
| ① /command token 未配置即放行 | webui 更糟：token 机制根本不存在——说明"先做功能后补门禁"是共同惯性，你的 ① 是半步之差 |
| ② confirm 未复查 offroad（TOCTOU） | **webui 的 `offroad_guard.py` 是正确答案**：`require_offroad()`/`require_disengaged()` 集中共享、**执行时逐请求复查**、返回结构化 error——mate v6 ② 可直接按这个形状收口 |
| ③ hub.onroad() 不查 poller 心跳 | webui `state_hub.py` 共享 SubMaster 带 valid 检查，有可借鉴处但未覆盖心跳语义 |

## 三、工程亮点（真正值得借的四样）

1. **集中式 offroad_guard**（`bridge/offroad_guard.py`，~30 行）——上一节，直接修 mate v6 ②。
2. **param_changes 哈希链审计**（`param_changes_service.py`）：参数变更落历史、**hash chain 可重走校验**（`verify_param_changes` 报第一处断链）+ `fingerprint baseline`（全部调参摘要对比基线）。mate v6 完全没有的**防篡改审计**能力，调参排障（"谁什么时候改了什么"）极有用。
3. **panel_catalog 数据驱动面板**：16 面板 schema 化（widget 类型/约束/分组），备份恢复按 catalog 作用域裁剪 + 尊重 `key+"Lock"`。你的 mate v6 param 白名单 21 键可演进成这个形状——加键不再改代码。
4. **PC mock 开发回路**：`dev/run_pc.py` + `WEBUI_DEV_PC=1` 门控 dev 路由（门控本身做得对，环境变量才注册）+ 右下角 SIM 模拟面板，全程 PC 开发、上车只做验收。你的 mate/webui 类项目值得复制这条开发回路。

另有小亮点：Display 面板的亮度/熄屏 API（与第三轮"bl_power=4 真下电"语义同源）；模型管理器 1:1 对齐车机；WebRTC 广角/长焦手动+按车速自动切换。

## 四、结论与建议

- **不建议整体移植**：功能与 mate v6 大面积重叠，而安全姿态远差于你已审查的 mate v6（你至少有 token+confirm 门禁方向）。把 webui 原样上车 = 把 mate v6 的三个问题放大一个数量级。
- **值得借的四样**（§三）按价值排序：offroad_guard（修 ②）→ 哈希链审计 → panel catalog → PC mock。
- **若将来想跑 webui 本体**（例如贪它的 WebRTC 观景/模型管理），最小改造清单：①token 门禁（对齐 mate v6）②`WEBUI_HOST=127.0.0.1` 默认 + ssh 隧道 ③CORS 白名单（删掉反射）④`reset_all_params`/`reboot`/`shutdown` 加 offroad+confirm ⑤ssh_api 从 bridge 摘除。改完这五条才算"可信局域网"级别。

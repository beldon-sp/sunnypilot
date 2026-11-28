# AGNOS 20.6 tici 全套方案：mouxancnb 三件套 vs 你的 beldon-op 线

**日期**: 2026-11-28
**重要发现**: 你自己的清单（`openpilot/system/hardware/tici/agnos.json`，AGNOS 20.6.102）已经指向 **`cnb.cool/beldon-op/agnos-builder`** 的 release——你这条线**已经在跑同款架构**（自有 builder + 固件托管 CNB + openpilot 清单指向）。所以本轮不是"从零参考"，而是对照 mouxancnb 的成熟度找差距。固件本体不可互换（你的 20.6.102 userspace 比他的 20.6.18 新，是 raylib post98 hardware-EGL 线；他的 system 是给 Toyota/raylib_nb 栈调的），**该吸收的是流程、工具和补丁，不是固件**。

---

## 1. mouxancnb 三件套架构

```
mouxancnb/agnos-builder        构建内核+system+boot+splash，打包四份清单
  │  发布 agnos-<版本> release（boot/system/清单/SHA256SUMS）
  │  发布 agnos-firmware release（XBL/ABL 等通用固件国内镜像 + splash）
  ▼
mouxancnb/openpilot（本库）     设备软件；同时托管 all-partitions-tici.json
  │  （flash 工具"预设"的来源，工具每次实时拉 master）
  ▼
mouxancnb/flash                网页刷机工具（EDL/qdl.js）
```

**清单体系（4 份，分工明确）**：
| 清单 | 分区数 | 用途 | 消费者 |
|---|---|---|---|
| `all-partitions-tici.json` | 33（GPT+固件+boot+system+userdata 预设） | 整机恢复/救砖，**清数据** | flash 工具（预设实时拉 openpilot master） |
| `all-partitions.json` | 32 | 同上，C3X/four | flash 工具 |
| `agnos_tici.json` | 7（xbl,xbl_config,abl,aop,devcfg,boot,system） | OTA 保数据升级 | flash 工具"OTA 模式" + 设备端 updater（与 `system/hardware/tici/agnos.json` **同内容同 URL**） |
| `agnos.json` | 7 | 同上，C3X/four | 同上 |

要点：
- generic 固件分区 URL 全部从 `commadist.azureedge.net` 重写到 CNB `agnos-firmware` release（国内直连）；
- `system.alt` 备用源指向**自家包**（同内容同 hash），不引用 comma stock——防止 fallback 拉到 hash 不匹配的东西。

## 2. 工程化程度差距（这部分是真价值）

### 2.1 发布流程加固（对 beldon-op 线直接适用）
- **逐资产 hash 复核**：CNB release 覆盖上传可能**静默失效**（PUT/verify 均 2xx 但内容没换）——发布脚本逐文件重试 + hash 复核，验收用 API 核对 `hash_value`，别只看构建 success。你的 release action 若有覆盖上传同样中招。
- **一致性门**（`09b4d14c5`）：launch_env.sh 的 `AGNOS_VERSION` 必须等于两份 OTA 清单指向的版本，否则发布**直接 FAIL**。这是防"manifest 冻在旧版而 launch_env 已 bump → updater ping-pong"的硬闸——正是你 11-19 AGNOS 升级事故的形状。staging 还有 version-aware 仲裁（semver 比较 master vs staging，防无条件回滚清单）。
- 构建期守卫：raylib wheel 版本不对 → 构建失败而非设备黑屏（20.6.4 全黑屏教训，`uv.lock` post106 黑屏 comma three）。你的 20.6.102 恰好是 raylib userspace bump——这类守卫对你尤其相关。

### 2.2 内核补丁（对 C3L 克隆有实义）
- **恢复 AGNOS 12.8 的 C3-only kernel config**（2 行）：`QPNP_COINCELL`（RTC 备份电池充电——没它主电掉电时钟重置，OTA 重启循环中观察到）+ `MSM_BCL_PERIPHERAL_CTL`（PMIC 电池电流/电压热传感）。驱动和 DT 节点都还在，纯 config 开关，带 config 断言。**你的 20.6 内核基座若没这两个开关，值得核对**。
- USB-PD 补丁链 + StarPilot 内核基座（BT 诊断包分类、hci_qca debug）。

### 2.3 flash 网页刷机工具（白嫖即可，不必自建）
- EDL(9008)/qdl.js 线刷，Windows 便携单 exe（内置 Chromium + Zadig 驱动 + `/mirror` 同源代理：cnb 防盗链 302 跟随 + 断点续传）+ Android APK；
- 两模式：整机恢复（33 分区，清数据）/ OTA 升级（7 分区保数据，A/B 双槽切槽）；
- **支持自定义清单 URL**——可以直接填你 beldon-op 的 `all-partitions-*.json`（如果你发布一份的话），工具本体零改动。

### 2.4 设备侧 updated/ 加固（openpilot 仓库内，可单独搬）
- `DisableUpdates` 时 idle-wait 而非 exit（防 manager respawn 风暴）、清理 device-local edits 再 check/fetch、AGNOS 20.6 无 git-lfs → prebuilt 用 `lfs.py` 拉、git_proxy 国内加速。

## 3. 你的现状对照（已验证）

| 项 | mouxancnb | 你（beldon-op 线） |
|---|---|---|
| AGNOS 版本 | 20.6.18（Toyota/raylib_nb 栈） | **20.6.102**（raylib post98 hardware-EGL，更新） |
| OTA 清单 7 分区 | ✅ 指向自家 release | ✅ 指向 beldon-op agnos-20.6.102 release |
| 通用固件国内镜像 | ✅ agnos-firmware release | ✅（7 分区 URL 全在 beldon-op，无 comma CDN 依赖） |
| `system.alt` 备用源 | ✅ 指自家包 | ❌ **无 alt**（CNB 单点故障 → OTA 无 fallback） |
| 整机线刷清单（33 分区） | ✅ + flash 工具预设托管 | ❌ 未发现（救砖走手工 edl？） |
| 发布一致性门/版本仲裁 | ✅ | ❌（launch_env 手工改版本号，`c5cbfb894 修改agnos 版本号` 类提交即手改痕迹） |
| 内核 COINCELL/BCL config | ✅ | ❌ **已实测确认缺失**（见 §6：全部 `=n`，且 DT 节点在本机 DTB 中不存在，补丁需 DTB 级修改） |
| flash 网页工具 | ✅ | ❌（未发现 beldon-op/flash） |
| launch_env 定制 | CI 烘焙 | 手工维护（DISABLE_DRIVER/SKIP_FW_QUERY） |

## 4. 建议吸收清单（按价值排序）

1. **发布一致性门**（半天）：在你 openpilot release action 里加 pre-push 校验——`launch_env.sh` 的 `AGNOS_VERSION` 必须等于 `agnos.json` 各分区 URL 里的 `agnos-<版本>`，不一致直接 fail。防 ping-pong 事故复发，改动最小收益最大。
2. **system.alt 备用源**（1h）：OTA 清单的 system 条目加 alt 指向另一份内容寻址的备份（例如另一个 release tag 或组织内备份库），避免 CNB 单点 + 覆盖上传静默失效时无 fallback。
3. **flash 工具接入**（配置级）：用 mouxancnb/flash 的工具 + 自定义 URL 指向你的清单。若要整机线刷清单，参照 `package_carrot_agnos.py` 的 all-partitions 打包逻辑（base manifest + 替换 boot/system + URL 重写）给自己生成一份 33 分区清单——你的设备是克隆机，救砖路径值得补齐。
4. **内核 config 核对**（已完成 → **已确认缺失，且比预想更麻烦**）：2026-11-28 实测 20.6.102 内核 `QPNP_COINCELL` / `MSM_BCL_PERIPHERAL_CTL` / `QTI_BCL_*` 全部 `=n`；且 `/proc/device-tree` 中 **coincell/bcl DT 节点不存在**——mouxancnb 的"2 行 config 补丁"以"驱动与 DT 节点都在"为前提，他的基座成立，本机 DTB 不成立。要在本机补齐需连 DTB 一起改（或从 12.8 的 DTB 移植节点），工作量从 1h 升级为内核+DTB 重编译级别，收益（掉电时钟不重置）与风险需重新权衡，**建议降级为"知道有这个坑，暂不动"**。
5. **updated/ DisableUpdates 加固**（可选）：若你有过 DisableUpdates 后 manager 反复重启的现象，搬 idle-wait 补丁。

## 5. 明确不搬的部分
- **固件本体**（Toyota system、splash、Toyota Space UI、raylib_nb 栈适配）：与你的 BYD/raylib DRM 直连栈不兼容，你的 20.6.102 更新更贴合你的 UI 线。
- **staging/version 仲裁全套**：你单分支单设备，简化成一致性门即可。
- Toyota 蓝牙试用链、reset zipapp 中文化等：与本分支无关。

---

## 6. 设备实测记录（2026-11-28 02:30，ssh comma@192.168.3.146）

| 项 | 实测结果 | 对文档结论的影响 |
|---|---|---|
| AGNOS 版本一致性 | `/VERSION` = launch_env = OTA manifest = **20.6.102**，slot `_b` | 三处一致，当前无 ping-pong；也证明"一致性门"要防的正是将来手改某一处时出现的错位 |
| 内核版本 | 4.9.103 | mouxancnb 基线同代 |
| COINCELL/BCL config | `QPNP_COINCELL` / `MSM_BCL_PERIPHERAL_CTL` / `QTI_BCL_PMIC5` / `QTI_BCL_SOC_DRIVER` 全部 `=n` | §4.4 确认成立 |
| coincell/bcl DT 节点 | `/proc/device-tree` 中**不存在** | "2 行 config 补丁"前提不成立，需 DTB 级修改，§4.4 已降级 |
| git-lfs | 未安装 | "AGNOS 20.6 has no git-lfs" 成立（prebuilt 走 lfs.py 的必要性确认） |
| 显示栈 | weston 进程数 0 | DRM 直连成立（与 updater 触摸坐标坑的历史结论一致） |
| I2C bus1 / IMU | bus1 无 sysfs 设备节点，但 **`/dev/i2c-1` 直读 0x6a `WHO_AM_I=0x6a` 正常应答**（0x6b NAK，即 dmesg 里 `i2c_geni a88000.i2c: i2c error :-107` 的来源）；用户态 sensord 手跑 8s 初始化无报错 | **硬件有 IMU，内核不枚举**——用户态 sensord 不受影响；但 mouxancnb 的 `imu_present()` sysfs 探测在本机**必然误报"无 IMU"**，详见 virtual-sensord 方案文档的修正条目 |
| launch_env 定制 | `DISABLE_DRIVER=1`、`SKIP_FW_QUERY=1` 现役 | 手工维护状态与既有认知一致 |

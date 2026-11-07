# BYD 宋 PLUS DM-i：panda 自建固件构建记录

> 时间：2026-09-26 · 配套文档：`docs/byd-panda-recovery.md`（排障过程）
> 本文记录自建固件的构建环境、源码基线选择、构建过程与当前状态。

## 一、为什么必须自建固件

| 固件 | 结论 |
|---|---|
| 官方 v0.10.1 固件 | ❌ 在此兼容板上 `assert_fatal(hw_type != UNKNOWN)` 故意挂起（板型 GPIO 不匹配任何官方型号）|
| 厂商固件（DEV-3a515542）| ✅ 能启动、CAN RX 正常；**但不含我们 openpilot 侧需要的 byd 安全模式 @35**（其 byd 为私有协议 @29）|
| **自建固件** | ✅ 唯一能同时满足"能启动 + 含 byd@35"的方案 |

构建源码基线的选择经过（重要）：

1. **0.9.x 时代基线**（commaai/panda `7287ff0c`，2024-06-12）——已验证可启动 + CAN RX 正常，但 SPI 协议是旧版，与 0.10.1 主机栈握手失败（`SPI: timed out waiting for ACK, waiting for 0x1f`），C++ pandad 连不上 → UI"无 PANDA"
2. **0.10.1 时代基线**（3dc2138，2025-08-19，F4 支持被删除前的最后提交）——该时代的 F4 板型检测只认 DOS，其余板型 `assert_fatal` 挂起，因此必须强制 DOS 板型。**实测结论（见第六节）**：此基线可构建，但 app 刷入后完全不枚举 USB（崩溃原因未深查，弃用）；最终走的是"0.9.x 基线 + 改 USB VID"方案。

## 二、构建环境（Mac ARM64）

> **持久化位置（2026-09-27 起）**：/tmp 重启会清空，构建树与工具链已复制到
> `~/Documents/op/`：`panda_fw_base`（0.9.x 基线 + safety_byd.h，**日常改固件用这个**）、
> `panda_fw_010`（0.10.x 基线备份）、`arm-tc`（交叉工具链）。
> 重建固件：`export PATH="$HOME/Documents/op/arm-tc/bin:$HOME/.local/bin:$PATH"`
> 后在 `~/Documents/op/panda_fw_base` 里执行第四节命令。

### 1. 交叉编译工具链

brew cask 安装会在后台卡死（需要 sudo 交互），**用官方 tarball 直下 + 断点续传**：

```bash
# Arm 官方服务器限速（~45KB/s），用循环续传直到完成
URL="https://developer.arm.com/-/media/Files/downloads/gnu/13.2.rel1/binrel/arm-gnu-toolchain-13.2.rel1-darwin-arm64-arm-none-eabi.tar.xz"
cd /tmp
until curl -sL -C - --retry 5 --retry-all-errors -o arm-gcc.tar.xz "$URL"; do sleep 3; done
mkdir -p /tmp/arm-tc && tar -xJf arm-gcc.tar.xz -C /tmp/arm-tc --strip-components=1
export PATH="/tmp/arm-tc/bin:$PATH"
```

总大小 129,557,320 字节 ≈ 129.6MB。GCC 13.2 编译 2024/2025 年的 panda 代码无 `-Werror` 问题。

### 2. 构建工具

```bash
uv tool install scons --force --with pycryptodome
# pycryptodome 是签名步骤（crypto/sign.py）必需
```

**三个坑**（都会导致构建中断）：
- `crypto/sign.py` 由 SConscript 以独立子进程调用，shebang 是系统 python3（无 Crypto）。
  修法：`SConscript:126` 的命令行前缀加 `{sys.executable}`（文件头部需 `import sys`）
- uv tool 重装后 site-packages 会重置，`--with pycryptodome` 必须与 `--force` 同用
- sign 步骤报 `ModuleNotFoundError: No module named 'Crypto'` 时优先查这里

### 3. 源码树

```bash
git clone https://github.com/commaai/panda /tmp/panda_fw_base
cd /tmp/panda_fw_base && git checkout 7287ff0c   # 0.9.x 基线（或 3dc2138 = 0.10.1 基线）
```

部署树（/data/openpilot）里的 panda 目录**没有板级源码**（只有 obj 产物），必须从上游拉。

## 三、byd 安全模式的移植

### 0.9.x 基线（旧式平铺结构）——已完成并验证

- 新建 `board/safety/safety_byd.h`：从主仓库 `opendbc/safety/modes/byd.h` 移植，
  适配老结构差异：
  - 统一 `SteeringLimits`（扭矩/角度共用，含 `max_steer`/`max_rt_interval` 字段）
  - 无 `current_safety_param` 全局 → 用 `static bool byd_angle_steering` +
    `byd_init()` 里 `GET_FLAG(param, ...)` 赋值（老结构惯例，参考 nissan）
  - `CanMsg` 无 `check_relay` 字段；`RxCheck` 用 `.frequency` 命名初始化
- 注册三处：`board/safety.h` 的 define 区（`SAFETY_BYD 35U`）、include、registry
- **AccState 编码修正**：Song 是 `1 = ACC_ACTIVE`（Han 是 3），此修正必须同步到
  固件版 safety_byd.h（曾因漏改导致 controls_allowed 永不置位）

### 0.10.1 基线（新结构）——直接复用主仓库文件

`opendbc/safety/modes/byd.h` 本身就是新结构，SConscript 的 `CPPPATH` 含
`opendbc.INCLUDE_PATH`，理论上直接拷入 `board/safety/` 即可；唯一要改的是
板型检测强制（见下）。

### 板型强制（两个基线都需要）

此兼容板的 GPIO 检测不匹配任何官方型号 → 官方代码 `assert_fatal(... UNKNOWN)`
挂起。固件层强制：

```c
// board/stm32f4/board.h 的 detect_board_type() 末尾（0.9.x 基线）：
hw_type = HW_TYPE_UNO;           // 0.9.x：UNO = USB 传输
current_board = &board_uno;

// 0.10.x 基线（board/stm32f4/board.h）：
hw_type = HW_TYPE_DOS;           // 0.10.x：DOS = SPI 传输（tici 内置）
current_board = &board_dos;
```

传输方式由板型决定：UNO → USB，DOS/TRES/CUATRO → SPI。**注意（实测更正）**：
3dc2138 时点 F4 的 `board_dos` 已是 `has_spi = false`（USB），SPI 只有 H7
（tres/cuatro）用；且此兼容板的 panda 实际是 **USB 接线**（lsusb 可见即证）。
曾把"SPI timed out"误判为接 SPI，实为 USB VID 不匹配（见第六节根因）。

## 四、构建命令

```bash
export PATH="/tmp/arm-tc/bin:$HOME/.local/bin:$PATH"
cd /tmp/panda_fw_base            # 或 /tmp/panda_fw_010
scons -j8 board/obj/panda.bin.signed
# 产物：board/obj/panda.bin.signed（DEV 签名，bootstub + app 签名链自洽）
# board/obj/version 应显示 DEV-<commit>-DEBUG
```

只构建主固件可跳过 libpanda 测试组件（其依赖 `gcc-13` 裸命令，未装会 Error 127）。

## 五、刷写流程（设备端）

```bash
# 1. 停 openpilot
sudo systemctl stop comma
# 2. （可选）跳刷开关：签名自洽后不需要；过渡期用于防止旧包覆盖
touch /data/panda_skip_flash    # 删除: rm /data/panda_skip_flash
# 3. 强制 DFU
python3 -c "from openpilot.system.hardware import HARDWARE; \
  HARDWARE.recover_internal_panda()"   # 需 capnp（/usr/local/venv/bin/python）
lsusb                            # 应见 0483:df11
# 4. DFU 刷 bootstub
sudo /usr/local/venv/bin/python -c "
from panda.python.dfu import PandaDFU
s = PandaDFU.list(); PandaDFU(s[0]).recover()"
# 5. ★ BOOT0 复位（关键！DFU 强制会把 GPIO134/BOOT0 拉高，不复位则永远回 DFU）
sudo sh -c 'echo 0 > /sys/class/gpio/gpio134/value'
# 6. 刷主固件（经 bootstub）
sudo /usr/local/venv/bin/python -c "
from panda import Panda
p = Panda(); assert p.bootstub
p.flash(fn='/tmp/panda.bin.signed')"
# 7. 验证
lsusb                            # 3801:ddcc = 我们的固件运行中（VID 已改，见第六节）
```

**Python 环境**：`/usr/local/venv/bin/python`（有 usb1/capnp）；SSH 非交互 shell
的 `python3` 没有 usb1，直接跑 `from panda import Panda` 会 ModuleNotFoundError。

## 六、根因更正与最终方案（2026-09-26 收口）

### 1. "0.9.x 固件 + 0.10.1 主机栈 = 无 PANDA" 的真正根因：USB VID

之前误判为"SPI 协议不匹配"。实际链路：

- python 库 `panda/python/__init__.py` 接受 **两个 VID**：`USB_VIDS = (0xbbaa, 0x3801)`
  → 所以 `Panda.list()`、CAN RX 测试、pandad.py 的连接/签名校验**一直正常**
- **C++ pandad（数据路径）只认 `idVendor == 0x3801`**（comma 注册的新 VID，
  见上游 `selfdrive/pandad/panda_comms.cc` PandaUsbHandle::PandaUsbHandle）。
  0.9.x 固件枚举 `bbaa:ddcc` → C++ 找不到 → 异常 → 兜底走 SPI → 死循环
  `SPI: timed out waiting for ACK` → UI"无 PANDA"
- C++ 是 USB 优先、SPI 兜底（`panda.cc`：try USB → catch → SPI），
  与 python lib `connect()` 逻辑一致
- 另：swaglog 里的 `SPI: timed out` 来自 `selfdrive/pandad/spi.cc`（python 侧
  cython 模块），读日志时注意区分层级

### 2. 0.10.1 基线实验结果（弃用）

3dc2138 + 强制 DOS + PYTHONPATH 挂主仓库 opendbc（含 byd@35）：
**构建一次通过**（-Werror 全绿，69800 字节，byd 符号齐全），
但刷入后 **app 完全不枚举 USB**（bootstub 正常 ddee，app 复位后无任何设备；
bootstub 也跑 detect_board_type，故崩溃在 app 专属初始化路径，未深查）。
DFU 流程可救回。设备 CAN 引脚配置 UNO 与 DOS 完全一致，排除板型配置差异。

### 3. 最终固件（当前在车运行）

**0.9.x 基线（7287ff0c）+ UNO 强制 + byd 移植 + `USB_VID 0x3801`**
（`board/config.h` 一行改动，源码树 /tmp/panda_fw_base）：

> 2026-11-08 起在车固件升级为 `DEV-9d0a0046-DEBUG`：在 7287ff0c 之上追加
> c5fcc9d9（byd 注册+板级配置）、fee17db6（BYD_PARAM_LONGITUDINAL 纵向移植
> + 透明 ACC 替换语义）、9d0a0046（health v17 + VID 0xBBAA→0x3801 + UNO 强制），
> 经 DFU 成对刷入（见第七节 2026-11-08 补充）。

| 验证项 | 结果 |
|---|---|
| 刷写（DFU bootstub → BOOT0 复位 → app）| ✅ 一次干净完成 |
| lsusb | ✅ `3801:ddcc`（app）/ `3801:ddee`（bootstub），**VID 已从 bbaa → 3801** |
| 版本 | ✅ `DEV-7287ff0c-DEBUG` |
| byd@35 双向 | ✅ `set_safety_mode(35,0)` 读回 35，无回落 SILENT（厂商固件死于此）|
| pandad.py | ✅ 1 panda found，**签名自洽**（1212766e… = expected），不重刷 |
| **C++ pandad 数据路径** | ✅ `panda.cc: connected to … over USB`（历史上首次）|
| pandaStates 心跳 | ✅ 10Hz，`pandaType: uno`，ignition 正常 |
| **实车 CAN RX（全链路）** | ✅ 点火状态下 15 秒 9563 帧（0x11f/0x123/0x320 等按频率流入）|

### 4. 遗留待办

| 项 | 说明 |
|---|---|
| 固件产物入库 | 3801 版 `panda.bin.signed` + `bootstub.panda.bin` 需提交进仓库
  （panda/board/obj/），否则 OTA/更新同步后签名失配 → pandad 自动刷回旧包；
  FIRMWARE_NOTICE.md 同步更新。设备端已手工预置（含 .uno.bak 备份）|
| 实车四级验证 | 台架全通后的既有流程：动态滚动 / 转向 / 跟车 / 干预 |
| 3dc2138 崩溃根因 | 未查（不影响现方案，留档备查）|

## 七、签名链说明

- DEV 构建：`certs/debug` 密钥签名，配套 dev bootstub（同一次构建产出），
  bootstub 校验 app 签名通过 → 签名链自洽
- `panda.bin.signed` 提交进仓库后，pandad 的期望签名（读自同文件）与运行固件
  一致 → 不再需要跳刷标志，OTA 固件更新通道正常
- 官方 bootstub（v0.10.1）与 DEV 签名 app 的兼容性未验证；刷写时始终
  bootstub + app 成对刷

### 2026-11-08 实证补充（重要更正）

- **"app 崩溃循环"结论是错的**：厂商 6e4111d4 bootstub + DEBUG 签名 app 的
  实际表现是 bootstub `fail()` 进 USB soft flasher——有 USB 枚举、
  `bootstub: True`、永不离开，与"app 启动即崩"完全同貌。app 从未运行过。
- **bootstub 无版本端点**（bootstub.c 不响应 0xd6），USB 侧无法查询 bootstub
  身份 → 唯一安全路径是**无条件 DFU 成对刷**：`recover_internal_panda()` 进
  DFU → `PandaDFU.program_bootstub(dev_bootstub)` + `reset()` → 经 bootstub
  `_flash_static` 刷 app → 验签。当晚实测一次通过。
- 成对刷结果（9d0a0046：bootstub eefd4cc8 + app af5174fc）：实车
  `set_safety_mode(35, 2)` 不回落、`safety_tx_blocked` 持续为 0（厂商注入器
  固件下 ≈50/s 增长）、pandad 签名接受不回刷。
- sunnypilot 仓库 `byd_long_flasher.py` 已改为 DFU 成对刷，
  `byd_long_bootstub.bin` + `byd_long_fw.bin.signed` 均入库；另注意 carrot 版
  python 库的 `p.health()` 带 health 版本校验、对 0.9.x 基线固件会抛
  RuntimeError——运行栈 health 走 C++ panda.cc（按回复长度分派三布局），不受影响。

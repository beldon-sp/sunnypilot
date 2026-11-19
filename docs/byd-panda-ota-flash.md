# BYD 宋 PLUS DM-i：panda 纵向固件 OTA 自动刷写调查与修复（2026-11-19）

配套构建文档见 `byd-panda-fw-build.md`（固件如何编译/签名）。本文记录：设备端自动刷写为什么一直失败、怎么修的、以及刷机后 LKAS 相关告警的解释。

## 一、症状链

1. 屏幕提示 **"缺少纵向控制固件 / Firmware rejects ACC_CMD - reflash or disable AlphaLongitudinal"**（`EventName.bydLongFirmwareMissing`）。
2. 机理：`AlphaLongitudinalEnabled=True` 但 panda 固件不放行 0x32D/E/F ACC 流 → `safetyTxBlocked` 累计超阈值 → selfdrived 看门狗（`selfdrived.py` BYD M1 gate）持续报 alert。
3. pandad 每次启动自动调用 `flash_byd_long()` 尝试经 DFU 配对刷写 `byd_long_fw.bin.signed`（9d0a0046），**全部失败于 "BYD panda did not enter DFU mode"**。

## 二、硬件与 USB 拓扑（关键事实，2026-11-19 实测）

- C3L 克隆机里的 BYD panda 是一块 **STM32F4 DOS/UNO 克隆 dongle，焊在设备内部、挂在设备自己的 USB 总线上**（`lsusb` 见 `3801:ddcc`，app 串号 `520023001051333038373636`；无 SPI 内部 panda）。
- **SoC 的 GPIO 124（STM_RST_N）/134（STM_BOOT0）只接 comma three 原装内部 panda 的引脚，到不了这个 dongle**。`recover_internal_panda()` 对它完全是空操作——旧 flasher 走 GPIO 强制 DFU 的路在这台机器上永远走不通。
- DFU 正确入口是 **USB 请求 `0xd1`（wValue=0，即 `Panda.reset(enter_bootloader=True)`）**：让 app 固件自己重启进 ST 系统引导。厂商固件和 9d0a0046 均支持（vendor 的 pandad 也是这条路：`host_can_pulse_boot0()` 对非 SPI dongle 返回 False，退到 `panda.recover()`）。
- **"快速启动"开启时，设备重启/关机不切断 USB 供电** → dongle 一直带电：进了 DFU 就停在 DFU，跨重启持久（多次重启后仍见 `0483:df11`）。"复位 ≠ 断电"。
- dongle 上电时 BOOT0 采样行为不定：多次整机断电后仍进 DFU；app 写好后也见过直接以 app 枚举（`3801:ddcc`）。
- **MCU 主动发起的 USB 状态变化（0xd1、dfu.reset 跳转）后，主机侧重新枚举可能很慢甚至需要一次真实断电**；另有证据表明并发干扰（pandad 的 DFU 恢复循环同时在敲同一设备）会加剧掉线。所有等待必须有界。

## 三、pandad 的隐藏炸弹（已修）

旧 `pandad.py` 启动循环对**所有** DFU 设备无条件 `PandaDFU(serial).recover()`：

- `recover()` 按 MCU 类型打开 `FW_PATH/bootstub.panda.bin`——board/obj **只打包了 H7 镜像，没有 F4 的**；
- F4 dongle 一旦停在 DFU，这里必然 `FileNotFoundError` → 被 while 循环捕获 → 下一轮再炸 → **pandad 永远起不来，openpilot 整个无法启动**（实测 pandad 48% CPU 疯狂重试）。

修复：按 MCU 类型门控——有打包镜像的（H7）才 `recover()`，F4 交给 `flash_byd_long()` 的 DFU 流程；且 `flash_byd_long()` 在 `Panda.list()` 为空时也要执行（DFU 停驻的 dongle 不会出现在 `Panda.list()` 里）。

## 四、关于 safetyParam 的误判史（避免重蹈）

- 历史上 pandad 日志出现过 `setting safety model: 35, param: 0`，曾被当成"固件缺 LONGITUDINAL"的旁证。**param=2 才是 alpha_long 的正确值**（`opendbc/car/byd/interface.py`：`safetyParam |= BydSafetyFlags.LONGITUDINAL`）。
- 2026-11-19 实测：dongle 刷入 9d0a0046 后，pandaStates 显示 `safetyModel=byd, safetyParam=2, safetyTxBlocked=0`——参数流本身没问题，告警根因就是固件不放行 ACC 流。
- 判断"固件是否支持纵向"必须看 `safetyTxBlocked` 计数（真实 50Hz ACC 流被拒），不能只看 `set_safety_mode` 是否"latch"。

## 五、固件版本清单（本机相关）

| 版本串 | 出处 | 状态 |
|---|---|---|
| `DEV-9d0a0046-DEBUG` | 本仓库 `sunnypilot/selfdrive/pandad/byd_long_fw.bin.signed`（sha256 `af5174fc…`）+ `byd_long_bootstub.bin`（`eefd4cc8…`） | **当前在车运行**（2026-11-19 经 DFU 直写刷入，签名验证通过） |
| `DEV-f8fe1c84-DEBUG` | 来源不明（非本地任何构建树），carrot 系带 acc inject 钩子的构建 | 刷机前 dongle 所跑；LKAS 正常、ACC 放行情况未定性 |
| `DEV-7287ff0c-DEBUG` | `~/work/op/byd_fw_build/`（2026-09-26） | 备选 carrot 系构建 |
| `DEV-6e4111d4-RELEASE` | `~/work/op/carrotpilot/byd/byd/panda/board/obj/` | 厂商 RELEASE 镜像 |
| `DEV-253ac12f-DEBUG` | `~/work/op/op_byd/panda/board/obj/`（2025-04） | 旧 vendor 构建 |

## 六、应急手动刷写程序（当前固件可用，无需依赖发版）

设备端一段脚本即可完成（参考仓库 `sunnypilot/selfdrive/pandad/byd_long_flasher.py` 新实现）：

1. 停 openpilot（`pkill -9 -f '[m]anager.py'; pkill -9 -f '[p]andad'`，注意 pkill 模式自匹配会杀掉自己的 ssh）。
2. 让 dongle 进 DFU：app 模式下发 USB `0xd1` wValue=0；或整机断电（慢枚举，等 1~2 分钟）。
3. DFU 内**一次会话直写 bootstub+app**：`program_bootstub(bootstub)` → 按 F4 扇区表 `erase_sector(1..N)` + `program(0x08004000, fw)`（设备 panda 库没有 `program_app`，用原语手写）→ `dfu.reset()` 跳转。
4. 验证：`Panda.get_signature()` 与 `Panda.get_signature_from_firmware(FW_PATH)` 一致；版本串 `DEV-9d0a0046-DEBUG`。
5. 若跳转后重新枚举不出来：整机断电再上电，多数情况直接以 app/DFU 枚举；以 DFU 枚举就重复第 3 步。

## 七、自动刷写新流程（本次代码改动）

`sunnypilot/selfdrive/pandad/byd_long_flasher.py` 重写：

1. **DFU 停驻优先**：`PandaDFU.list()` 非空时先处理 dongle——持久状态标记（`/data/persist/byd_long_fw_state`，记录已验证写入的 fw+bootstub sha 对）已匹配则仅 `dfu.reset()` 跳转；否则 DFU 直写配对后跳转并写标记。非 F4 的 DFU 设备一律不碰（归 pandad 打包镜像恢复管）。
2. **app 模式**：签名一致 → 直接返回（不消耗配额）；不一致 → USB `0xd1` 软进 DFU（有界等待 25s）→ DFU 直写配对 → 跳转 → 有界等待重枚举并验签（20s）。GPIO 路径删除。
3. 配额逻辑（`/dev/shm` boot_id 标记，每 boot 最多 3 次写操作；跳转不消耗配额）。**2026-11-20 复查修正**：
   - 配额门只能挡"擦写"分支，**绝不能挡 DFU 跳转**——否则配额耗尽时一个已刷好固件对的 dongle 会被永远卡在 DFU，openpilot 起不来（等于复发）。跳转分支在配额检查之前执行。
   - `_read_attempts()` 增加**新鲜度检查**（标记 mtime 超过 1 小时视为过期）：本机实测 `/dev/shm` 会被快照还原、boot_id 可能跨重启不变（2026-11-19：7 月 mtime 的标记匹配当前 boot_id），若不检查，配额会跨重启累积、3 次后**永久锁死**刷写——这正是"重刷/换设备后问题复发"的路径。

配套测试 `tests/test_byd_long_flasher.py` 全绿（24 项，2026-11-20）。

## 七A、全新设备/重刷设备的预期行为（自愈路径）

CNB release 包含两个固件 bin（`release_files.py` 全量拷贝 tracked 文件，bin 为普通 git blob），因此换设备刷本仓库固件链路成立。预期时序：

1. **Boot 1**（dongle 跑厂商固件）：签名不一致 → `0xd1` 进 DFU。本机实测该克隆机 MCU 侧 USB 重枚举可能不回来（需要断电）→ 刷写标记失败 1 次，openpilot 无 panda（屏幕会报 panda 掉线）。
2. **断电重启 → Boot 2**：dongle 以 DFU 枚举（fast boot 停驻或冷启动 strap）→ pandad 检测到无打包 F4 镜像不碰它 → `flash_byd_long` DFU 直写 bootstub+app → `dfu.reset()` 跳转 → app 枚举回来 → 写持久标记。**一次断电循环内自愈完成**。
3. 此后每次启动：app 在跑 → 签名一致直接返回；若 dongle 又停在 DFU（断电采到 BOOT0 高）→ 标记命中 → 仅跳转，秒级恢复。

所以：换设备刷完后**看到 panda 掉线不要慌，断电重启一次即可**；若两次断电后仍无 panda，按第六节手动程序排查。

## 八、刷机后 LKAS 相关告警解释（2026-11-19 实测）

- 刷完首次启动屏幕出现 **"LKAS Fault: Restart the Car"**（`EventName.steerUnavailable`）——固件切换后 EPS 对新的 0x316 报文流短暂不适应；**熄火重新上电后消失**，属过渡现象。
- 若 OP 介入转向后该告警反复出现，优先怀疑固件与车辆 CAN 拓扑不匹配（例如缺 acc inject 钩子导致 0x32D 双发送者冲突），此时回退 carrot 系固件（`DEV-7287ff0c-DEBUG` 或找回 `DEV-f8fe1c84-DEBUG`）。

## 九、遗留问题

1. **f8fe1c84 镜像本地无副本**：dongle 原厂在跑的 DEBUG 构建来源不明（疑为厂商安装工具或 carrot OTA 写入）。如需回退，先用本机现有 7287ff0c/6e4111d4。
2. ~~**屏幕闪烁（摄像头画面闪缩）**~~：**已消失（2026-11-20 用户确认）**。此前 UI 日志的 `Failed to duplicate frame fd when creating EGL image: Bad file descriptor` + FPS 掉到 41 疑似与纵向固件缺位/openpilot 异常状态相关；纵向固件刷好、链路打通后不再复现，无需单独排查。若日后复发再按相机 buffer fd 生命周期 / weston / rootfs 方向排查。
3. fast boot 下 dongle 断电行为仍不完全可控；自动流程已覆盖"DFU 停驻"，但"app 停驻 + 需要重刷"仍依赖 0xd1 成功枚举。
4. CNB release 打包需确认 `byd_long_fw.bin.signed`/`byd_long_bootstub.bin` 随包分发（本次设备上两文件已随包到位）。

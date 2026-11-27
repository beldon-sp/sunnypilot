# 中文字体管线：mouxancnb 演进版 vs 本分支现状

**日期**: 2026-11-28
**状态**: ✅ 已完成 —— 第 3 节的 ①②③ 三点已全部落地并提交（commit `6d0392b70`，byd-tici 分支），第 4 节明确不搬的部分维持原判。
**结论先行**: 本分支的 OpFont 管线本来就是从 mouxan 那边移植来的（`6f59bc1df tici: port OpFont pre-baked CJK font pipeline from mouxangithub`）。mouxancnb 仓库里是这条管线的**后续演进**：核心重写（放弃运行时加载图集）是为他那套 nanobind 绑定做的绕路，本地用不上；但**两个运维级修复是本地当前就存在的真 bug，建议直接抄**。

---

## 1. mouxancnb 的演进史（3 个阶段）

### 起点：为什么重写
他的新栈（AGNOS 20.6 + raylib_nb/nanobind 绑定）：
- `load_font_ex` 只认裸 TTF 数据，喂 `.fnt` 路径会读文件但返回空字形集 → 非 ASCII 全渲染 `?`；
- `Font` 结构体不暴露可写 glyphs/recs → 运行时组装图集无路可走；
- python 层 ffi shim 返回 plain list 不是 cffi struct → 8cb865eab 的 BMFont 加载器方案 6 分钟后被自己推翻（该提交保留仅作诊断记录）。

### 终态（7e7fac7c4）
- **放弃运行时加载 .fnt 图集**，所有 .fnt 目标直接路由到"源 OTF 运行时烘焙"（load_font_ex 的 TTF 路径，即本地已有的 CJK fallback 路径）；
- UNIFONT 烘 **all-language labels 图集**：ASCII + EXTRA_FONT_CHARS + languages.json 各语言原生显示名（修语言选择器里 한국어/ไทย 变 `?` 的问题）；
- TTF 权重（Inter）继续 load_font_ex + 失败检测，失败回退 labels 烘焙。

### 两个运维修复（与绑定无关，纯流程）
| commit | 内容 | 本地状态 |
|---|---|---|
| `736fcd095` | launch_chffrplus.sh 检测 OpFont 图集缺失（未跟踪 scons 产物被 overlay 更新的 git clean whiteout）→ manager 启动前跑 process.py 重生成（CPU-only），避免每次开机全量 CJK 烘焙拖慢启动 | **本地没有**（launch_chffrplus.sh 无任何字体逻辑） |
| `f7fca7fc2` | updater zipapp 的 pack.py 白名单只有 .ttf → .otf 全丢 → 设备上烘焙兜底无源字体 → 中文 tofu | **本地同款 bug**（见下） |

---

## 2. 本分支现状（逐项已验证）

本地已经是**完整的前一代管线**，且绑定是 comma cffi pyray（`rl.load_font` 支持 .fnt、`rl.ffi` 可用于烘焙），没有 mouxan 新栈的绑定缺陷：

```
openpilot/selfdrive/ui/SConscript            # 烘 OpFont 图集（per weight × language，未跟踪产物）
openpilot/selfdrive/assets/fonts/process.py  # 生成器（已适配 raylib 5.5 LoadFontData，f2ff4340a）
  fonts/: NotoSansCJK{sc,tc,jp,kr}-Regular.otf + unifont.otf + Thai + Inter 全家
openpilot/system/ui/lib/application.py:
  font()          :935  rl.load_font 加载 .fnt；失败检测(texture.id 异常)→按语言烘焙 NotoSansCJK
  _bake_fallback_font()  load_font_ex + ffi codepoints（po 文件字符集 + ASCII + EXTRA_FONT_CHARS）
  fallback_font() :1012 per-language 图集 + 缺字形 pending 队列，offroad 才 rebake
  font_fallback() :214  非拉丁文本 → 活动语言 fallback 图集
```

链路完整，zh UI 正常工作。**mouxan 的核心重写（7e7fac7c4）本地不需要**——那是 nanobind 绑定的绕路。

---

## 3. 建议吸收的 3 个点（按价值排序）

> **✅ 2026-11-28 已全部实现，commit `6d0392b70`**（byd-tici）。验证记录：py_compile / bash -n 通过；pack.py 模拟 copy 确认 .otf 入包；图集 glob 检测两分支（在/缺失）行为正确。剩余收尾：下次 release 打包核对 zip 内 fonts/ 含全部 .otf。

### ① pack.py 加 .otf（一行，必修）✅ 已完成
`tools/release/pack.py:15`：
```python
EXTS = ['.png', '.py', '.ttf', '.capnp', '.json', '.fnt', '.mo', '.po']
```
**没有 `.otf`** → 打包丢 NotoSansCJKsc-Regular.otf / unifont.otf。当前正常路径靠 .fnt 图集能活，但一旦设备上图集缺失/不匹配（更新换树、加语言），运行时烘焙兜底会因源字体不在包里而**直接失败 → 全 tofu**。这正是 mouxan 在实机上截过图的事故。修法：加 `'.otf'`。

### ② launch 启动前图集兜底（11 行，建议抄）✅ 已完成
本地图集同样是未跟踪 scons 产物。若 release 流程或 overlay 更新造成图集缺失，每次开机都要全量 CJK 烘焙（启动明显变慢）。把 736fcd095 的检测块抄进 launch_chffrplus.sh（manager 启动前）：
```sh
if ! ls "$DIR"/openpilot/selfdrive/assets/fonts/OpFont-*-*.* >/dev/null 2>&1; then
  echo "[fonts] font atlases missing, regenerating"
  (cd "$DIR" && "$PY" openpilot/selfdrive/assets/fonts/process.py >> /tmp/font_bake.log 2>&1) ||
    echo "[warn] font atlas regeneration failed; UI will bake fallbacks at startup"
fi
```
注意本地 process.py 已适配 raylib 5.5 绑定（f2ff4340a），可直接跑。

### ③ labels 图集补全语言名（可选，小）✅ 已完成
本地 UNIFONT/labels 权重缺失时按 active lang 的 po 烘焙，语言选择器里**其他语言**的原生名（한국어/ไทย/中文）不在 zh po 里 → `?`。mouxan 的做法：labels 烘焙字符集额外并入 `languages.json` 的 display names（ASCII + EXTRA_FONT_CHARS + 各语言名）。改动在 `_load_or_bake_cjk_font` 加几行。

---

## 4. 明确不搬的部分
- 7e7fac7c4 的"全 .fnt → 运行时烘焙"路由：为 nanobind 无可写 Font 结构而生，本地 cffi 绑定 `rl.load_font(.fnt)` 工作正常，照搬反而丢掉预烘焙的性能优势（GPU 图集 vs 启动时烘焙）。
- 8cb865eab 的 python BMFont loader：已被作者自己推翻，仅存档价值。

## 5. No-Go 纪律
①② 均为打包/启动脚本层修复，不触驾驶栈，风险极低；改完跑一次 release 打包核对 zip 内字体清单即可，不涉及 onroad 行为。

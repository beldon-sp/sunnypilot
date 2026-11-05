# 模型清单（Small / Big Models）— byd-tici C3L 克隆机

> **快照日期：2026-11-06**，抓取自 sunnypilot-models gh-pages 清单（URL 见下）。
> 清单在远端滚动更新，最新列表以设备 UI「设置 → 模型」实际显示为准；本文用于了解可选范围与排查时的 ref 对照。

## 工作机制

模型由 ModelManager（`sunnypilot/models/manager.py`）从两个清单拉取，对应 UI 的两个槽位：

| 槽位 | source | 清单 URL | 运行位置 |
|---|---|---|---|
| Small Model | `qcom` | `https://raw.githubusercontent.com/sunnypilot/sunnypilot-models/refs/heads/gh-pages/docs/driving_models_v25.json` | 本机 Adreno GPU（tinygrad QCOM 后端），无需外设 |
| Big Model | `chestnut` | `https://raw.githubusercontent.com/sunnypilot/sunnypilot-models/refs/heads/gh-pages/docs/driving_models_chestnut_v28.json` | 外接 chestnut USB 加速狗（`USB+AMD:LLVM`） |

**可选判定**（`sunnypilot/models/fetcher.py` / `helpers.py`）：

1. `bundle.minimumSelectorVersion == REQUIRED_JSON_VERSION`（本树 `sunnypilot/models/helpers.py:15` 为 **21**）；
2. runner 本树能跑（本树 `modeld_v2` 支持 tinygrad；Runner 枚举见 `cereal/custom.capnp:174`：snpe / tinygrad / stock）。

当前两份清单所有 bundle 均满足（全部 runner=tinygrad、selector version=21），因此**清单是全量可选的**。清单没有按设备（tici/tizzbird/mici）区分的字段——qcom pkl 在设备上按后端 JIT 编译，tici 机型通用。

**下载镜像**（`helpers.py` `_MIRROR_HOST_MAP`，仅下载期生效，sha256 校验兜底）：

- `huggingface.co` → `hf-mirror.com`
- `raw.githubusercontent.com/sunnypilot/sunnypilot-models/...` → `cdn.jsdelivr.net/gh/sunnypilot/sunnypilot-models@gh-pages/...`
- 可用 `ModelManager_MirrorDisabled` 参数关闭镜像。注意：国内网络下 jsDelivr 曾出现 SSL 握手失败告警（swaglog 有记录），失败后回退原源。

**默认模型**（`sunnypilot/models/model_name.py`）：

- Small 默认 = `CD210`（即清单中的 C210M）
- Big 默认 = `Mountain Dew Model v3`（MDMV3）

## 本机适用性（C3L 克隆机，tici）

- **Small（77 个）**：全部真可用，跑在本机 Adreno 上。
- **Big（20 个）**：**全部不可实际运行**。需要 chestnut 加速狗（USB VID/PID `0xADD1:0x0001` 或 `0x3801:0x0001`，product 串 `custom <fw>-CLEAN`，见 `common/hardware/usb.py`），C3L 克隆机没有。无狗时 `chestnutPresent=false`，实际开车永远是 small 兜底（`model_info.py` `carrying_model()`）；Big 槽位仅可选中/下载。

## Small Models（qcom 槽位，77 个）

`release` 9 个 / `master` 13 个 / `development` 55 个；Legacy Models 文件夹 7 个为旧版 10Hz 模型（generation 3/4），其余均为 20Hz（generation 12）。

| 短名 | 名称 | 环境 | 20Hz | 文件夹 | ref |
|---|---|---|---|---|---|
| LAV2 | LAv2 (January 24, 2024) | development | ✗ | Legacy Models | `f9d5fc7dd9b763356a1201d96c6953ed4562226e` |
| CHV2 | Certified Herbalist v2 (February 19, 2024) | release | ✗ | Legacy Models | `a4d53d378d4331dc7780e5e60845ea1731c48341` |
| RH | Recertified Herbalist (March 01, 2024) | release | ✗ | Legacy Models | `063623602c9a3cefe70d2c661b40ef7a15f73d43` |
| DA | Duck Amigo (March 18, 2024) | release | ✗ | Legacy Models | `5f3960a3a7689903a4da5e80b7e70cf0056acad3` |
| WD40 | WD40 (April 09, 2024) | release | ✗ | Legacy Models | `9691393bde34efb8be6fccef497d6d17758c209c` |
| NDV2 | North Dakota v2 (April 29, 2024) | release | ✗ | Legacy Models | `375978d40b976bf9e82ab74f267846a56b7656a6` |
| ND | Notre Dame (July 01, 2024) | development | ✗ | Legacy Models | `e0c89a36da8e6a06ab85c4e74c44f70dd910e1fe` |
| FOF | Filet o Fish (March 07, 2025) | release | ✓ | Release Models | `d807b5c476658dba7760d30949baa619074c4a4e` |
| SPVM | SP Vikander Model (May 03, 2025) | development | ✓ | Custom Merge Models | `d134fdd7d51ba84848866c0265e37508c38f9366` |
| TR7 | Tomb Raider 7 (May 07, 2025) | development | ✓ | 2025 Pre World Models | `0fb4aafa35896e831bd58a636a9a1bb4a1442743` |
| SGO | Secret Good Openpilot (May 16, 2025) | development | ✓ | 2025 Pre World Models | `80a636e4952633886c65973d3323e58ec87ed320` |
| KD | Kerrygold Driving (June 08, 2025) | development | ✓ | 2025 Pre World Models | `dc7a92ea630e4f6053082fc25eca83938927b91e` |
| LCD | Liquid Crystal Driving (June 21, 2025) | development | ✓ | 2025 Pre World Models | `b55af0de7fecdb4af073c01b77b04b8306a4e92d` |
| VFOF | Vegetarian Filet o Fish (June 21, 2025) | development | ✓ | 2025 Pre World Models | `c790d3f58ad32cf15f384e6018d942b2348f0c5b` |
| TR13 | Tomb Raider 13 (June 27, 2025) | development | ✓ | 2025 Pre World Models | `eceeaabcebcf58716ae4a018930793d89c96777b` |
| ATR | Aggressive TR (June 28, 2025) | development | ✓ | 2025 Pre World Models | `d54a5888cf6165b30e4bdda514ab9057473cd7f8` |
| TR14 | Tomb Raider 14 (June 30, 2025) | development | ✓ | 2025 Pre World Models | `194cdbcebf2d47011d2edbf39a4ae492aa1aa338` |
| SPD | Simple Plan Driving (July 07, 2025) | development | ✓ | 2025 Pre World Models | `f523f104eafc49dccb447aea4f7c3f666ef5626c` |
| DTRV5 | Down to Ride v5 (July 08, 2025) | development | ✓ | 2025 Pre World Models | `5b405920ec9511e146432de08171d6f93295248b` |
| TR15 | Tomb Raider 15 (July 11, 2025) | development | ✓ | 2025 Pre World Models | `ed38ca8cc998f0a2b47cde29da11b782e4551e8b` |
| LTR14 | Le Tomb Raider 14 (July 14, 2025) | development | ✓ | 2025 Pre World Models | `c2fa07b82c2e09964e4b6dccb3e4465a2e4ad138` |
| TR16 | Tomb Raider 16 (July 21, 2025) | development | ✓ | 2025 Pre World Models | `e4b6a2736580dc427473bcf8a6e3b08eb352edd4` |
| SL2 | Space Lab 2 (July 26, 2025) | release | ✓ | Release Models | `d3a0378ae601f61307a53d1173c251ee36c56317` |
| SL3 | Space Lab 3 (August 08, 2025) | master | ✓ | Master Models | `659898d553eb68c981036811e1b92f6557d4e494` |
| DTRV6 | Down to Ride v6 (August 12, 2025) | master | ✓ | Master Models | `a6cf39c07a67dbc346af03010093fe4788c402d0` |
| UVDTRM | UV + DTR Model (August 13, 2025) | development | ✓ | 2025 World Models | `079d0461604632719e73dc5b6a5eb9897c624da2` |
| SPM | Steam Powered Model (August 16, 2025) | master | ✓ | Master Models | `f1b8f510b2c554191ca15cebe35f2c5b304cf23c` |
| GMV3 | gWM Model v3 (August 22, 2025) | development | ✓ | 2025 World Models | `93b26a91a62b69b809162ad2bf65e36be2158dc1` |
| FM | Firehose Model (August 31, 2025) | master | ✓ | Master Models | `b74f5189a74446015c0cf78a4a9f0134a347ae3b` |
| FBW | Fly By Wire (September 04, 2025) | development | ✓ | 2025 World Models | `c4488e3411285f6fc3d008bca885e05731fd75c2` |
| NM | Nevada Model (September 07, 2025) | master | ✓ | Master Models | `3193eac5e385aa010694a8ac192ff38ffe000193` |
| GWMV5 | gWM v5 (September 09, 2025) | development | ✓ | 2025 World Models | `e3ee440f7d5ac12c50db6381e292a55a69b745a7` |
| GWMV6 | gWM v6 (September 20, 2025) | development | ✓ | 2025 World Models | `91a1cea814f76d605d5b15de9634a3c8f79df509` |
| GWMV8 | gWM v8 (September 28, 2025) | development | ✓ | 2025 World Models | `96e7b310b164b55fb73abdd505b66fb36630718e` |
| TCPM | The Cool Peoples Model (October 03, 2025) | development | ✓ | 2025 World Models | `5306f07d05a4f84a632ce908ec5ce34993e73693` |
| NNM | North Nevada Model (October 07, 2025) | development | ✓ | 2025 World Models | `74d156ee827863aa02567228a4a48931e604a41c` |
| NNMV2 | North Nevada Model V2 (October 08, 2025) | release | ✓ | Release Models | `83b81b83cdf76a577946a1567f4644e1a018443c` |
| NID | Nuggets in Digon (October 09, 2025) | development | ✓ | 2025 World Models | `13e79e9fad60c19e751e4f9ab0538d39f1bb54dd` |
| TCPMV2 | The Cool Peoples Model v2 (October 09, 2025) | development | ✓ | 2025 World Models | `19084132cd3956413a0fdfcdc18971a55df5e0ff` |
| TCPMV3 | The Cool Peoples Model v3 (October 10, 2025) | release | ✓ | Release Models | `5eb912c025a2207c7d428deee74ded49e5905527` |
| GWMV9 | GWM V9 (October 21, 2025) | development | ✓ | 2025 World Models | `f2413040a8560c0c17c18353a3c75124a2f09d17` |
| CGWM | Cool GWM  (October 24, 2025) | development | ✓ | 2025 World Models | `94dd3bd9107cd413ed2418f9e6c0cf805a9ca495` |
| NDM | Neurips Driving Model (November 12, 2025) | development | ✓ | 2025 World Models | `266b642180f0e9278e1db46ccf8c1a2a8dae4bb3` |
| NDMV2 | Neurips Driving Model v2 (November 12, 2025) | development | ✓ | 2025 World Models | `c00ebef3269415d80800d10d255479e4006bf152` |
| PM | Planplus Model (November 28, 2025) | development | ✓ | 2025 World Models | `671ac1084cedc82bca49f82bdd8c27f4d2506190` |
| DSM | Dark Souls Model (December 03, 2025) | master | ✓ | Master Models | `ba783b9f913de089816598aa367a30499727a0ce` |
| PLANPLUS | Planplus v2 (December 11, 2025) | development | ✓ | 2025 World Models | `50c78a9dd670a305ca898b96245cadbc2ecdc1a6` |
| DSMV2 | Dark Souls Model v2 (December 11, 2025) | master | ✓ | Master Models | `5ff0e48f90d662d8c9d5061adc63e90c198c9b81` |
| MACROSTI | Macrostiff (January 01, 2026) | development | ✓ | 2026 World Models | `d70b13931793bc0e4d8efd36128dd66f227a3f81` |
| WMIV9 | WMI V9 (January 07, 2026) | development | ✓ | 2026 World Models | `8950897d7e4d2ba2426b2b31cf29f25e87a3d4ba` |
| SCM | SC Model (January 08, 2026) | development | ✓ | 2026 World Models | `e4a4b4b1adf2d19fedab4d195faa382b061fa754` |
| WMIV12 | WMI V12 (January 13, 2026) | master | ✓ | Master Models | `7e3fd7a63c5a09c3fabe108b1b62bba3cf684878` |
| **C210M** | **CD210 Model (February 03, 2026)** ← Small 默认 | development | ✓ | Release Models | `5b6436a90cf6902b8aaa71c2b6f3d7164d8ae391` |
| OPM | off policy model (February 04, 2026) | development | ✓ | 2026 World Models | `d90758cfaecb2cf13d9098c6aade37e68d6034d2` |
| OMV2 | Off-policy Model v2 (February 20, 2026) | development | ✓ | 2026 World Models | `ae34a24c59d85df7efad5fde2b760fb6d0b9dd6e` |
| OPMV3 | Off Policy Model v3 (February 26, 2026) | development | ✓ | 2026 World Models | `7e548dd765873bed301c0a19cfe10c3ca6be2bbe` |
| OMV4 | Off-Policy Model V4 (February 27, 2026) | development | ✓ | 2026 World Models | `25abcc49fce2f6268d8cabf759e31c626edbf584` |
| OMV5 | Off-Policy Model v5 (March 14, 2026) | development | ✓ | 2026 World Models | `b390b98c5a359c293fdc2501af79b19867fcdac4` |
| PM | Pop Model (March 20, 2026) | development | ✓ | 2026 World Models | `6f71783a8a8faa07ddaeef5bbb6809b4f4f44a15` |
| PMV2 | Pop Model v2 (March 24, 2026) | development | ✓ | 2026 World Models | `62bf6fb072880905a4c490f0f4f4a6b3c23346ec` |
| OPM | OP Model (April 01, 2026) | development | ✓ | Master Models | `cb327933002bc1a00bf60a3c20af2eb7a5f653e5` |
| OPM7 | OP Model 7 (April 03, 2026) | development | ✓ | Master Models | `052692b25d63c5ddda276b5c2271383b6aff129f` |
| OPM8 | OP Model 8 (April 07, 2026) | development | ✓ | 2026 World Models | `eae2a73e0ac600d7f0342fc4397568e0733fe6e2` |
| OPMV10 | OP Model v10 (April 17, 2026) | development | ✓ | 2026 World Models | `2671624103b9a37c53680dd9110d9a957f61d043` |
| OPM10V2 | OP Model 10 v2 (April 18, 2026) | development | ✓ | 2026 World Models | `bbde5ddb5a0ee35c98a962029355f9a95403a825` |
| OPM10V3 | OP Model 10 V3 (April 19, 2026) | development | ✓ | 2026 World Models | `7d4c295c27bc3f72727c49ec73b98e45745b588d` |
| OPM16D | OP Model 16 Deep (June 03, 2026) | development | ✓ | Master Models | `f02d134f40f5e7be22b182af21b438915a47600e` |
| MRLV2 | Michael RL V2 (July 18, 2026) | development | ✓ | 2026 Deep RL Models | `37b38bf738edfb1daa6875255f581e5fc9b0a258` |
| RLM | Rebel Legion Model (July 22, 2026) | development | ✓ | Master Models | `2895346746634d7eec0ee749f946c87039948a25` |
| GYHUM | Get Your Hopes Up Model (July 25, 2026) | development | ✓ | 2026 Deep RL Models | `574735edc6e1aafdc2a69395f9a32e7f5cc4b62b` |
| RHM | Rebellious Hope Model (July 27, 2026) | development | ✓ | Master Models | `93f5aa469a72b7621aef7da7901c100e0113e4d9` |
| RDFM | RDF Model (August 05, 2026) | development | ✓ | 2026 Deep RL Models | `a95e2c25cae5fbf1afba7628bfb7acc4af59e0cc` |
| RDFMV2 | RDF Model V2 (August 11, 2026) | development | ✓ | 2026 Deep RL Models | `35703097905a122c9f3ddf0d12889b4873d7e2a2` |
| TSFM | Terrible Super Fantastic Model (August 12, 2026) | development | ✓ | 2026 Deep RL Models | `4d911346cde4e0d2978a625f31679808284cc19d` |
| KV | Kumars Vibe (August 14, 2026) | development | ✓ | Custom Merge Models | `a992d64eb604af3effb5b6ce948da52507ae9de9` |
| FP | Falling Phoenix (August 14, 2026) | development | ✓ | Custom Merge Models | `eff0854aad2a8908a6522382c78af96ce3810668` |
| TSFDOM | Terrible Super Fantastic Do Over Model (August 15, 2026) | development | ✓ | 2026 Deep RL Models | `0394d7284cfc229f772ee4fa73ff724ba8245110` |

## Big Models（chestnut 槽位，20 个）

全部 20Hz、generation 12、development 环境，**本机无 chestnut 加速狗，均无法实际运行**。
清单编译用 tinygrad ref（`tinygrad_ref` 字段）：small 清单 `5d186bdb…`、big 清单 `52e8c3e8…`（与 small 不同，chestnut 走 USB+AMD 后端）。

| 短名 | 名称 | 环境 | 20Hz | 文件夹 | ref |
|---|---|---|---|---|---|
| LM | Lebowski Model (July 01, 2026) | development | ✓ | Master Models | `fa0c6876d3cf070e91e25e5353ceadc68a5b3285` |
| TT | Tee Time (August 18, 2026) | development | ✓ | 2026 Deep RL Models | `ff27d6220e2869ede7ee3c83e7893ec06cb75551` |
| TTTTFBRL | TTTTFBRLM (August 18, 2026) | development | ✓ | 2026 Deep RL Models | `1d12f404b54dc8c5cadb73669ae27399ba35ba43` |
| BM | BMRLNAP Model (August 20, 2026) | development | ✓ | 2026 Deep RL Models | `7e3ff7e914a40ac25399a2caedbce3580fdde6a2` |
| IDM | I Desire Model (August 21, 2026) | development | ✓ | 2026 Deep RL Models | `6691f163a4ed8a9c4e2ca95efc29c04478ec0250` |
| HBM | Happy Birthday Model (August 24, 2026) | development | ✓ | 2026 Deep RL Models | `db06f5f36caa379e112bafd38423e37b66fa4149` |
| BMV2 | BMRLNAP Model v2 (August 24, 2026) | development | ✓ | 2026 Deep RL Models | `ba37c02c7c64225b8aec79411d1a8c2f5c1aa343` |
| BMV3 | BMRLNAP Model v3 (August 26, 2026) | development | ✓ | 2026 Deep RL Models | `47377ac2f726651fc1470eb725cf73418307e5aa` |
| SM | Sad Model (August 28, 2026) | development | ✓ | 2026 Deep RL Models | `30de303d5ffb63957f8acbdb256fd4c7d360455a` |
| BMV4 | BMRLNAP Model v4 (August 30, 2026) | development | ✓ | Master Models | `f877d7a0ccc3cce943c76e285214c020cd65c899` |
| BMV6 | BMRLNAP Model v6 (September 01, 2026) | development | ✓ | 2026 Deep RL Models | `9d683c06518c0358fb402f38468a7030700c38ac` |
| CTM | Cinque Terre Model (September 04, 2026) | development | ✓ | 2026 Deep RL Models | `68b5f8e48602f4f88041efd7de6c99e97fda454e` |
| CTMV2 | Cinque Terre Model V2 (September 08, 2026) | development | ✓ | 2026 Deep RL Models | `37bfa1413edcdc2e8844984b83727c33f81d8f46` |
| CTV3M | Cinque Terre V3 Model (September 17, 2026) | development | ✓ | Master Models | `bf3e3631b3f91d92a1020a5e0dd4298b93ff4244` |
| MDM | Mountain Dew Model (October 10, 2026) | development | ✓ | Master Models | `6f4c0faf1f85132734e94aa39c67143f823b9324` |
| **MDMV3** | **Mountain Dew Model v3 (October 14, 2026)** ← Big 默认 | development | ✓ | Master Models | `1e219d7785cfb7b7343c7f2366347fc86e7ce5f2` |
| TTM | Train Track Model (October 16, 2026) | development | ✓ | 2026 Deep RL Models | `e46b35b0dcce35761ba28ada1a7df6ae36589c30` |
| FFM | Flip Flop Model (October 23, 2026) | development | ✓ | 2026 Deep RL Models | `35c5406aa5b03c51a6246a1f09ff84464b2a6b96` |
| LSM | Light Speed Model (October 29, 2026) | development | ✓ | 2026 Deep RL Models | `e4acf619d4eb8b726fc405c264109310ac2951df` |
| LSMV2 | Light Speed Model v2 (October 29, 2026) | development | ✓ | 2026 Deep RL Models | `c7cbe5de6a9dae4bab032bc5faeb24a248c41f8f` |

## 排查速查

| 参数 / 位置 | 用途 |
|---|---|
| `ModelManager_ActiveBundle` / `ModelManager_ActiveBundleChestnut` | 当前选中的 bundle（qcom / chestnut 槽位），JSON 序列化的 ModelBundle |
| `ModelManager_DownloadRef` | 排队下载的 bundle ref（即上表 ref 列） |
| `ModelManager_ModelsCache` / `ModelManager_ModelsCache_Chestnut` | 清单缓存（1 小时过期）；置 `ModelManager_LastSyncTime*` 为 0 可强制刷新 |
| `ModelManager_ActiveJson` | 当前生效的清单 URL 记录 |
| `Paths.model_root()` | 模型文件落盘目录（tici：`/data/media/0/models` 附近） |
| 模型文件缺失/损坏 | ModelManager 校验 sha256，`_bundle_needs_reset` 会清掉失效槽位回落默认模型 |

相关代码：`sunnypilot/models/{helpers,fetcher,manager,default_model,model_name}.py`、`selfdrive/ui/sunnypilot/model_info.py`、`selfdrive/ui/sunnypilot/layouts/settings/models.py`（tici UI）、`selfdrive/modeld/helpers.py`（chestnut 检测）。

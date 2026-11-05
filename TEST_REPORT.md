# explore 智能本地路由 — 实施与测试报告（历史实测）

> ⚠️ **设备相关性声明**：本报告中的所有硬件、模型、显存、速度、上下文档位数据，
> 都是 **2026-11-06 在一台特定测试设备上的历史实测结果**，用于证明当时链路可用。
> 它们**不是项目的固定配置、不是能力边界、也不是对其它设备的要求**。
> 当前项目是配置驱动的：槽位模型、GPU 数量/品牌/后端、上下文上限、端口
> 全部由 `settings.yaml` 决定（详见 `PROJECT_INTRO.md` / `OPERATIONS.md`）。

- 时间：2026-11-06 00:11 – 01:43（GMT+8）
- 后续审计轮：2026-11-06 02:24 – 02:52，另做了真机真模型全量审计、故障注入、重启恢复与 CI 实测，**结果见 `AUDIT_REPORT.md`**；本报告仍只记录 00:11–01:43 的历史重写测试。
- 当时的测试设备：Ubuntu 22.04.5 / 双卡 NVIDIA（共 40G 显存）/ CUDA
- 独立工作区（当时路径）：`/home/liuda/work/explore-router`
- 状态：重写完成；当时三档上下文矩阵 + 速度基准 + 基础回归全部通过

---

## 1. 当时的环境（历史快照，非运行要求）

| 项 | 当时实测 |
|---|---|
| GPU | 2 张卡，共 40960 MiB 显存 |
| 后端 | CUDA；未安装 ROCm |
| Ollama / Redis | 均未安装 |
| Python | 3.10.12，缺 `python3.10-venv`，依赖用 `pip --target` 装进工作区 |
| NAS | 远程无法解析名称，本机 UNC 可读，采用流式直传 |

测试期间设备显存独占；未 disable 任何服务、未改单元文件、未删文件。

## 2. 当时的模型配置（示例，非固定）

| 槽位角色 | 当时填充 | 说明 |
|---|---|---|
| 前台 `resident: true` | 轻量 3B Instruct Q6_K（2.7G） | 可换成任意 3B/4B 小量化模型 |
| 中档 | 9B Q4_K_M（5.4G） | 可换 |
| 重档 | 35B MoE A3B Q4_K_XL（21G） | 可换成其它 MoE 或稠密模型 |
| embedding | ONNX bge 中文模型，1024 维 | 路径可配 |

> 当时前台曾误选「有效 4B / 物理约 8B」的模型，后更正为真 3B。这属于**当时那次配置选择**，
> 与项目结构无关；现在换模型只改 `settings.yaml`。

## 3. 当时的两处执行期适配

1. 用设备现成的 `llama-server`（CUDA 版 llama.cpp）代替 Ollama：公网下载 Ollama 二进制实测需数小时。现在 `runtime.server_bin` 可配，接口隔离在 `app/llama.py`。
2. 去掉 Redis / Celery，改单进程 asyncio 队列 + SQLite：单队列顺序执行使槽位天然互斥，消除旧项目的配额泄漏、状态不闭环等问题；不装系统服务、不自启。

## 4. 分档上下文矩阵（当时设备）

每档重启服务并跑同一套 23 项断言（功能链路、槽位切换、同档零切换、进度应答、结果原样递回、上下文恒定、三模型速度基准、日志 `n_ctx_slot` 一致）。

| 档位 | 断言 | 三实例 `n_ctx_slot` | 当时稳态显存 | 当时空闲 | 结果 |
|---|---|---|---|---|---|
| 32K | 23/23 | 一致 | 27384 MiB | 12978 MiB | 通过 |
| 64K | 23/23 | 一致 | 30036 MiB | 10326 MiB | 通过 |
| 96K | 23/23 | 一致 | 32688 MiB | 7674 MiB | 通过 |

原始结果曾写入 `logs/matrix_*.json`。**换设备后请用 `CTX_MATRIX` 选适合该设备的档位重测，不要照搬这三档。**

### 4.1 当时 64K/96K 的 OOM 与修复（现已成为可配置项）

首次跑高挡时重档模型启动即退出，日志显示 `cudaMalloc out of memory`：常驻前台的 f16 KV cache 随 N 增大挤占了显存。当时的修法是 KV cache 精度量化 + 重档跨卡分层比例偏向空闲卡。

**现在这两项都是 `settings.yaml` 的配置**：`context.kv_cache` 与 `slots.<key>.placement`；`spread` 的比例默认按「各卡空闲显存 − 前台预估占用」自动算，也可显式写 `split_ratio` 覆盖。换设备时调整配置即可，代码里没有相关常量。

配置驱动版完成后又复测了 32K / 64K / 96K 三档（69 项断言）全部通过，速度约 141 / 97 / 121 tok/s，与改造前一致 —— 说明配置外置没有破坏链路。

## 5. 当时的生成速度（历史基准，非性能指标）

直接读运行时 `timings.predicted_per_second`（token/s）：

| 槽位 | 32K | 64K | 96K |
|---|---|---|---|
| 前台 | 141.0 | 141.4 | 143.4 |
| 中档 | 97.3 | 97.4 | 96.9 |
| 重档 | 121.3 | 120.9 | 120.4 |

当时结论：CUDA 加速正常，达到「接近 100 tok/s」的预期；上下文增大对常规短 prompt 的生成速度几乎无影响，主要影响显存。**这些数字只代表当时的卡与当时的模型**。

其它当时实测：前台加载约 3.1s、重档约 6–7s（冷启动曾达 21s）；前台即时回复 10 条连续平均约 0.4s、无悬挂任务、队列深度 0。

## 6. 当时的基础回归

| 套件 | 结果 |
|---|---|
| `test_smoke.py` | 11/11 |
| `test_slot_switch.py` | 12/12 |
| `test_stability.py` | 10/10 连续请求 |

合计三档矩阵 **69 项** + 基础回归 **23 项断言 + 10 条连续请求**，当时全部通过。
测试脚本现已改为从 `/config` 动态读取端口、槽位名与档位名，换模型/换设备后无需改测试。

> 2026-11-06 02:24 起的审计轮在修改后再次复测：三档矩阵 69/69、API 审计 hard 27/27、故障注入 10/10、重启恢复 10/10、CI 集成 `INTEGRATION_OK`。详见 `AUDIT_REPORT.md`。

## 7. 当时修复的问题清单（历史）

| # | 问题 | 处理 |
|---|---|---|
| 1 | 新版 uvicorn 与系统旧 `websockets` 冲突 | `websockets>=15` 装进工作区 |
| 2 | GGUF 是 thinking 模型，正文落在 `reasoning_content` | `enable_thinking=false` + `/completion` 兜底 + 空输出报错 |
| 3 | embedding tokenizer 参数用错 | 改用 `enable_truncation`，实测 1024 维 |
| 4 | 缺 `python3.10-venv` | `pip --target` 隔离安装 |
| 5 | 高挡位下重档 OOM | KV 精度 + 分层比例（现为配置项） |
| 6 | 槽位切换失败后状态不一致 | 失败清空 `current`/`server` 并写 `last_error` |
| 7 | 前台误选「有效 4B / 物理 8B」模型 | 换成真 3B（现由配置决定） |
| 8 | 轻量前台指令遵循弱、任务完成后查不到进度 | API 层查最近任务并附 `[后台任务进度]` |

## 8. 过时内容检查与清理（本轮）

用户要求检查文档是否有过时/写死内容，逐份核对结果如下：

| 文档 | 发现的过时/易误解内容 | 处理 |
|---|---|---|
| `PROJECT_INTRO.md` | 把双卡硬件、3B/9B/35B 模型、固定 GPU 编号与分层比例写成项目事实；把三档显存/速度写成通用能力 | 改为「角色 + 示例配置 + 历史实测」；新增配置结构、槽位可替换、硬件自适应章节 |
| `OPERATIONS.md` | 环境要求写死某型号双卡与 CUDA；配置项写成代码常量；`-ts` 比例、`q8_0` 写成固定参数；换模型流程要求改代码 | 改为支持范围 + `settings.yaml` 字段表；新增换模型、单 AMD 卡适配、校准流程；排障表更新 |
| `README.md` | 架构图与表格写死具体模型和端口 | 改为角色视角，标注「当前示例配置，可替换」，指向 `settings.yaml` |
| `TEST_REPORT.md` | 历史实测数据未标注设备相关性，易被误读为项目约束 | 顶部加设备相关性声明，各表标注「当时/示例」，实现细节改为配置项 |
| `PROJECT_PLAN.md` | 需求与历史记录中出现早期硬件/档位表述 | 保留为历史演变记录，追加配置可移植性修订说明 |
| `settings.yaml` | 此前不存在，配置散落在 `config.py` 与文档中 | **新增为唯一配置源**，模型/硬件/上下文/端口全部外置 |
| 代码 | `config.py` 写死模型文件名、GPU 编号；`llama.py` 判断 `devices == "0,1"`；`main.py`/`router.py` 写死槽位名；测试写死 `cuda`、端口、N | 全部改为配置驱动；`/config` 暴露生效配置；测试动态读取 |

**当前代码中不再出现**具体模型文件名、参数量、GPU 编号、卡数、品牌型号、固定端口或固定上下文数值；这些只作为 `settings.yaml` 的**示例值**或本报告的历史数据存在。

## 9. 交付物与恢复

- 配置：`settings.yaml`
- 文档：`PROJECT_INTRO.md`、`OPERATIONS.md`、`TEST_REPORT.md`、`README.md`、`AUDIT_REPORT.md`
- 代码：`app/`、`run.sh`、`requirements.txt`
- 测试：`tests/`（smoke / slot / stability / matrix / quick_check / audit_api / audit_long_context / audit_single_gpu / audit_faults / audit_restart + CI 脚本），结果 `logs/*.json`
- CI：`.github/workflows/ci.yml`、`tests/ci_static.sh`、`tests/ci_integration.sh`

```bash
./run.sh stop          # 停本项目
./run.sh start --wait  # 再次运行
```

> 全程未删除任何目录内容、未 disable 服务、未配置开机自启。

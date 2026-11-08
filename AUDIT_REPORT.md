# explore 智能本地路由 — 全量审计与 CI 报告

> 审计时间：2026-11-06 02:24 – 03:35（GMT+8，含长上下文与单卡阈值补充轮）
> 目标机：Ubuntu 22.04.5 / 双卡 NVIDIA RTX 3080 20G（共 40960 MiB）/ CUDA
> 项目版本：2.4.0（配置驱动版）
> 审计方式：**真机、真模型、真进程故障，不使用 mock / 模拟数据**

## 1. 审计范围与方法

| 层次 | 做法 | 是否使用模拟数据 |
|---|---|---|
| 静态审计 | 逐文件阅读 `app/`、`settings.yaml`、`run.sh`、`tests/`，核对配置项是否真正生效 | 否 |
| 配置校验 | 用真实 YAML 文件 + 子进程导入，验证错误配置 fail-fast、档位改名可用 | 否（真实解析器） |
| API 功能 | 对运行中的服务发真实 HTTP 请求，检查状态码、字段、真实模型输出内容 | 否 |
| 内容正确性 | 数学题、排序算法、路由解释，检查回复是否真的包含正确答案/关键词 | 否（真实推理） |
| 并发/队列 | 10 路并发 `/chat`；连续提交重档任务；`QUEUE_MAXSIZE=1` 验证 429 | 否 |
| 长上下文/长文本 | 10 轮 × 1.2K 字多轮累积 + 随机标记召回；20K/50K 字长输入；连续 3 次约 900 字长回复 | 否（真实推理） |
| 显存阈值/单卡 | `CUDA_VISIBLE_DEVICES=0` 限制为单卡 20G，验证重档不可用、自动降级、不 OOM | 否（真实可见显存） |
| 故障注入 | 对真实 `llama-server` 进程发 `SIGKILL`，观察前台/后台自愈 | 否（真实进程） |
| 重启恢复 | 真实 `run.sh stop` / `start`，检查 pending/processing 任务归宿 | 否（真实停机） |
| 上下文矩阵 | 32K / 64K / 96K 各重启一次，跑同一套 23 项断言 + token 速度 | 否（真实模型） |
| CI | 静态层在目标机实跑；GPU 集成层用独立端口/数据目录完整跑通 | 否 |

**结论先行**：核心链路真实可用，硬性审计项全部通过；CI 静态层与 GPU 集成层均实跑通过。仍有三类遗留：AMD ROCm 单卡路径未真机验证、9B/35B 模型输出质量偏短、无鉴权/流式/日志轮转。

## 2. 审计环境（实测）

| 项 | 实测值 |
|---|---|
| GPU | 2 × RTX 3080 20G，驱动 550.107.02，共 40960 MiB |
| 后端 | CUDA（`/health.backend=cuda`，`gpu_count=2`） |
| Python | 3.10.12，依赖装在 `pylib/`（固定版本） |
| 运行时 | `/opt/llama.cpp/build/bin/llama-server`（llama.cpp，CUDA） |
| 前台 | 3B Instruct Q6_K（2.7G） |
| 中档 | 9B Q4_K_M（5.4G） |
| 重档 | 35B MoE A3B Q4_K_XL（21G，双卡分层 `[1,2]`） |
| Embedding | ONNX bge 中文，1024 维，CPUExecutionProvider |
| 上下文 | `num_ctx=32768`（`auto(total_vram=40960MB)`），KV `q8_0` |

## 3. 功能可用性矩阵

| 功能 | 验证方式 | 结果 |
|---|---|---|
| `/health` 契约 | 后端、N、前台、槽位、队列、embedding、显存字段齐全 | 通过 |
| `/config` 契约 | 3 档位、映射、唯一 resident、限额字段 | 通过 |
| `/gpu` | 返回 2 张卡与显存 | 通过 |
| 简单任务直答 | “2+2” → `process_mode=direct`，回复 `4` | 通过 |
| 中档异步 | `prefer_tier=mid` → `chosen_model=mid`，结果非空且相关 | 通过 |
| 重档异步 | `prefer_tier=heavy` → `chosen_model=moe`，含排序算法名 | 通过 |
| 进度应答 | 任务 processing 时问“进度怎么样了” → 附 `[后台任务进度]` 真实状态 | 通过 |
| 结果原样递回 | `final_reply` 与 `instant_reply` 不同，槽位为后台模型 | 通过 |
| 性能指标 | `prefill_ms` 为真实 prompt 耗时，`release_info` 含 tok/s | 通过 |
| 多轮记忆 | 先记 7391，再问 → 回复 `7391` | 通过 |
| 多段长上下文 | 10 轮 × 1.2K 字，随机标记问第 8 轮 → 准确返回 | 通过 |
| 早期轮次召回 | 12 条历史窗口外的第 1 轮 → 模型明确“无记录” | 预期失败（窗口限制） |
| 长输入（20K 字） | 前台即时回复 + 后台 heavy 任务完成 | 通过 |
| 长输入（50K 字，96K 档） | 前台 16.9s 响应 + 后台 heavy 任务完成 | 通过 |
| 连续长回复 | 3 次约 900 字要求，实际 1042–1265 字，6–18s/次 | 通过 |
| 自动历史预算 | 10 轮多轮，`context_usage` 估算总量 14477/14709 ≤ 32768/98304；历史自动保留 18 条 / 12641–12873 字 | 通过 |
| 单卡小显存降级 | 单卡 20G 下 moe 标 `unavailable`，heavy 自动走 mid，未 OOM | 通过 |
| 后台结果回读 | 完成后问“刚才的排序算法” → 能列出（偶发先编造“处理中”） | 通过（质量观察） |
| 会话隔离 | 两个 session 的任务归属、`/task_result.session_id` 正确 | 通过 |
| 并发 10 路 | 10/10 HTTP 200、即时回复非空，最大 2004.8ms | 通过 |
| 队列深度 | 重档积压时 `/health.queue_depth` 可见（瞬时抓取不稳定，见 §7） | 观察 |
| 队列上限 | `QUEUE_MAXSIZE=1`，连续 5 次 → `[200,200,429,429,429]` | 通过 |
| 重启恢复 | 重启时 processing → failed 且说明“服务重启”；pending → 重新入队 | 通过 |
| 前台自愈 | `SIGKILL` 前台进程 → 下一次 `/chat` 自动重启，3.3s 回复 `OK` | 通过 |
| 后台自愈 | `SIGKILL` 后台进程 → 同档任务触发重启并完成 | 通过 |
| 输入校验 | 空输入 400；超长 413；非法 `prefer_tier` 400；未知任务 404；`limit=0` 422 | 通过 |
| 查询 URL | `query_url` 使用请求的 host，实际 GET 返回 200 且 task_id 匹配 | 通过 |
| 上下文恒定 | `/chat`、`/task_result`、`/health`、`/config`、三个实例日志的 `n_ctx_slot` 全一致 | 通过 |
| embedding | 启动即 1024 维，`/health.embedding.enabled=true` | 通过 |

## 4. 审计发现与修复

### P0 — 真实可用性/可移植性

| # | 问题 | 证据 | 修复 | 验证 |
|---|---|---|---|---|
| 1 | `run.sh` 与 `run_matrix.sh` 写死 `/home/liuda/work/explore-router` 和固定端口 | `check_portability.py` 直接命中 | 路径改为脚本所在目录；端口从 `settings.yaml`/环境变量读取；`stop` 只杀命令行含本项目目录的进程 | 静态检查 23 个文件 0 违规；CI 独立端口启动成功 |
| 2 | 服务重启后 `pending`/`processing` 任务永久卡死 | 代码审查：`lifespan` 只建队列，无恢复逻辑 | `store.reset_inflight()`：processing 标记失败并说明原因，pending 重新入队 | 真实 stop/start：任务 `failed` + “服务重启，任务中断”，不卡死 |
| 3 | 前台模型进程崩溃后不自愈，服务长期降级 | 代码审查：`/chat` 只捕获异常返回文案 | `ensure_front()` 健康检查 + 受锁重启 + 30s 冷却 | SIGKILL 后 3.3s 自动恢复，`/health.frontend=ready` |
| 4 | 分级档位名 `simple/mid/heavy` 写死在 `classify()`，与“槽位可换”矛盾；错配静默 fallback | `router.py` 原实现 | `routing.tier_order` 配置化；启动校验档位映射/阈值/resident/设备/比例 | `quick/standard/deep` 改名配置导入成功；4 类错误配置 fail-fast |
| 5 | 超长输入无上限，可能撑爆 SQLite/模型 | 代码审查 | `routing.max_input_chars`（默认 `num_ctx*1.5`），超限 413 | 49153 字符 → 413 |
| 6 | 后台队列无上限，可能无限堆积内存 | 代码审查 | `runtime.queue_maxsize`，满时提前 429 | `QUEUE_MAXSIZE=1` → 429 |
| 7 | 后台结果追加为第二条 `assistant`，历史角色不交替，3B 的 Jinja 模板返回 500，调用层静默降级 `/completion` | `front.log` 大量 `conversation roles must alternate`；多轮长上下文召回失败 | `store.append_message` 合并连续同角色；`get_history` 规范化并丢弃窗口开头的 assistant | 随机标记多轮召回通过（32K/96K 各 15/15）；`front.log` 无该异常 |
| 8 | 历史固定 12 条、输入上限按 `num_ctx*1.5`，长窗口利用率低且可能超过上下文 | 代码审查 + 长上下文测试 | 历史/输入/输出/余量统一按 `num_ctx` 自动预算；超限 413；响应暴露 `context_usage` | 32K/96K 长上下文 15/15，估算总量始终 ≤ N |
| 9 | 16G 单卡加载 35B MoE 会 OOM，没有显存可行性判断 | 显存规划 + 单卡测试 | `slots.vram_mb` + `budget` + `vram_plan`；不可用槽位标记并自动降级；前台不可用 fail-fast | 单卡 20G 11/11；双卡三槽位均可用 |

### P1 — 可靠性/ correctness

| # | 问题 | 修复 | 验证 |
|---|---|---|---|
| 7 | 后台最终结果不进入会话历史，下一轮前台无法回读 | 完成后追加 `[后台结果]` 到 `messages` | 多轮回读测试（warn）能列出算法 |
| 8 | 进度词“怎么样”会误伤正常提问，且历史任务无时间窗 | 强/弱进度词分离；弱词要求有活跃任务；`progress_window_s=1800` | 任务中问进度附带真实状态；正常提问不附旧任务 |
| 9 | SQLite 连接只提交不关闭，长期运行句柄堆积 | `_db()` 上下文管理器显式 commit/close | 连续测试无异常 |
| 10 | `/health` 在 async 端点同步调 `nvidia-smi`，阻塞事件循环 | `asyncio.to_thread` + GPU 快照 2 秒缓存 | 并发 10 路、健康检查正常 |
| 11 | `prefill_ms` 实际是总耗时，指标语义错误 | 改取 `timings.prompt_ms`；`release_info` 记录总耗时与 tok/s | 实测 `prefill_ms≈289ms`，总耗时 6869ms，104.3 tok/s |
| 12 | `query_url` 硬编码 `127.0.0.1`，远程客户端不可用 | 用 `request.base_url` 生成，另给 `query_path` | 返回的 URL GET 成功 |
| 13 | `budget.*`、`slots.*.keep_alive` 是死配置；`weight_mb` 注释与实际不符 | 删除死配置；修正注释（参与多卡自动分层扣减） | `check_config` + 文档字段表同步 |
| 14 | 依赖未固定版本，CI 不可复现 | `requirements.txt` 固定目标机实测版本 | 静态检查强制 `==` |
| 15 | 测试写死端口/日志目录，无法隔离运行 | `BASE`/`API_PORT`/`LOG_DIR`/`DATA_DIR` 可覆盖；CI 用独立端口与数据目录 | CI 集成层实跑通过 |
| 16 | 模型缺失/端口占用错误不清晰；调用层无重试 | 启动前检查并带日志尾部；连接错误/5xx 重试一次 | `missing_model`/`port_busy` 断言通过 |

### P2 — 可观测性/工程化

| # | 问题 | 处理 |
|---|---|---|
| 17 | embedding 启动时 `dim=0`，健康检查看不出可用性 | 启动 warmup，`/health` 立即 1024 |
| 18 | `run.sh stop` 按端口杀进程可能误伤 | 只杀命令行包含本项目目录的进程，其它打印 skip |
| 19 | 文档存在写死路径、已删配置、旧测试数量、“无输入上限”等过时内容 | 本轮逐份修订（见 §9） |
| 20 | 无 CI，改动靠手工回归 | 新增 GitHub Actions 静态层 + GPU 自托管集成层，本地脚本可复现 |

## 5. 测试结果（本轮实测）

### 5.1 API 功能审计

- 硬性项：**27/27 通过**
- 观察项：4/6 通过
  - `heavy_answer_detail`：35B MoE 对“三种排序算法各一句”只回 22 字，内容正确但过短（模型质量问题）
  - `queue_visible`：任务快速完成时抓不到瞬时队列深度（队列上限已由 429 硬验证）

### 5.2 故障注入

- **10/10 通过**
- 前台 `SIGKILL` → 3.3s 自愈；后台 `SIGKILL` → 同档任务触发重启并完成
- 模型缺失、端口占用均 fail-fast，错误信息可读

### 5.3 重启恢复与队列上限

- **10/10 通过**
- 重启时 processing 任务标记 `failed`，错误信息含“服务重启，任务中断”
- `QUEUE_MAXSIZE=1` 连续 5 次：`[200, 200, 429, 429, 429]`，被拒任务不落库为 pending

### 5.4 三档上下文矩阵

每档重启服务，跑同一套 23 项断言：

| 档位 | 断言 | 三实例 `n_ctx_slot` | 稳态显存 | 空闲 |
|---|---|---|---|---|
| 32K | 23/23 | 32768 ×3 | 27384 MiB | 12978 MiB |
| 64K | 23/23 | 65536 ×3 | 30036 MiB | 10326 MiB |
| 96K | 23/23 | 98304 ×3 | 32688 MiB | 7674 MiB |

合计 **69/69**。

### 5.5 连续请求稳定性

- 96K 档、10 次连续请求：**10/10**，平均 463.4ms，最大 556.5ms，无悬挂任务、队列 0、N 不漂移

### 5.6 CI

- 静态层：`STATIC_OK`（编译、shell 语法、13 项配置校验、可移植性 23 文件、文档一致性、ruff 0.17.3 全通过）
- GPU 集成层：`INTEGRATION_OK`
  - smoke 11/11、32K 矩阵 23/23、API 审计 hard 27/27、故障 10/10、重启 10/10、稳定性 10/10
  - 使用独立端口 18110/18120/18121、独立 `DATA_DIR`/PID/日志，未触碰生产实例
  - 长上下文审计已加入集成脚本；本轮 32K/96K 为单独实测（见 5.7）

### 5.7 长上下文与长文本专项（补充轮）

此前矩阵只覆盖短/中任务与 320-token 速度基准，本轮补充了多轮累积、长输入与连续长回复：

| 档位 | 多轮 10×1.2K 字 | 随机标记召回 | 长输入 | 连续 3 次长回复 | 硬性结果 |
|---|---|---|---|---|---|
| 32K | 10/10 回复，avg 5.5s；历史 18 条/12641 字 | 第 8 轮、第 1 轮均通过 | 20K 字，前台 1.9s，后台完成 | 1096–1265 字，10.5–18.6s | 15/15 |
| 96K | 10/10 回复，avg 3.3s；历史 18 条/12873 字 | 第 8 轮、第 1 轮均通过 | 50K 字，前台 17.3s，后台完成 | 1042–1222 字，6.3–8.3s | 15/15 |

**自动阈值**：`context_usage.estimated_total_chars` 最大 14477（32K）/14709（96K），远低于 `num_ctx`；长输入 20K/50K 时历史预算自动收紧到 5469/21460 字，估算总量 20614/50614，仍未越界。

**发现并修复 P0**：后台结果作为第二条 `assistant` 写入历史，导致 `system` 之后角色不交替，3B 的 Jinja 模板返回 500；调用层静默降级到 `/completion`，多轮结构化上下文实际失效。修复后 `front.log` 无该异常，随机标记（如 `MARK-8-D089E15F`）能真实召回，而不是按规律推断。

**限制**：历史预算按 `num_ctx` 自动计算，不再固定 12 条；32K 下可保留 18 条近期消息，但超出预算的早期轮次仍不可召回（模型会明确说“无记录”）。多轮 prefill 变长会让单轮延迟从约 3s 升到约 5.5s，这是召回与延迟的取舍，可调小 `history_budget_ratio`。

### 5.8 单卡/小显存专项（20G 近似）

用 `CUDA_VISIBLE_DEVICES=0` 让服务进程只看到一张 20G 卡，模拟未来单卡设备：

- `gpu_count=1`、`total_vram=20480MB`、`num_ctx=32768`；
- `vram_plan`：front 3800MB 可用、mid 7600MB 可用、moe 26000MB **不可用**（need 26000 > usable 13813MB）；
- heavy 请求 → `selection_reason` 含“moe 显存不足，降级到 mid”，任务 `completed`、`chosen_model=mid`，`release_info` 保留降级说明；
- **11/11 通过**，free 9680MB，前台仍 ready，无 OOM。

说明：这是单卡 20G 近似，不是真实 16G A 卡；16G 下 front+mid 仍可用（约 11.4G），moe 同样会被标记不可用并降级。

单卡模式下除降级专向外，三套常规回归也各跑了一遍（期望值按 `vram_plan` 动态生成）：

| 套件 | 单卡 20G | 关键点 |
|---|---|---|
| `quick_check.py` | 15/15 | 中档异步与 5K 长输入均落在 mid；无 500/OOM/角色交替异常 |
| `test_smoke.py` | 11/11 | complex 请求 `selection_reason` 明确写“moe 显存不足，降级到 mid” |
| `test_slot_switch.py` | 11/11 | moe 不可用时校验“降级到 mid 且切换计数不变”，不再要求切到 heavy |

双卡复跑 smoke 11/11、slot 12/12（mid→moe→mid 真实切换），确认上述测试改动无回归。

## 6. 性能与显存（本轮实测）

直接读 llama-server `timings.predicted_per_second`：

| 模型 | 32K | 64K | 96K |
|---|---|---|---|
| 3B 前台 | 140.6 | 139.9 | 139.4 |
| 9B 中档 | 97.4 | 97.2 | 97.5 |
| 35B MoE | 121.4 | 120.6 | 121.6 |

- 前台加载 3.2–3.3s；中档 3.2–3.3s；重档 6.1–7.2s
- 后台任务总耗时（含切换/加载/生成）约 6.9s，prefill 289–292ms，生成 104–106 tok/s
- 10 路并发简单请求：全部 200，最大 2004.8ms（前台单实例串行，符合预期）

## 7. 遗留风险与未验证项

| 项 | 说明 | 建议 |
|---|---|---|
| AMD ROCm 单卡 | 本轮无 AMD 硬件，**未真机验证**；`gpu._rocm()` 已支持 `GPU[i]` 前缀与旧版总量格式，配置解析单卡路径可用 | 在目标 AMD 机器跑 `ci_static.sh` + 短集成（3B 前台）确认 |
| 模型输出质量 | 9B 常回 15–22 字；35B 对“各一句”只回 22 字；3B 在结果回读时偶发编造“处理中” | 换更强的 9B/35B 量化或调 `frontend.system_prompt`/任务包要求 |
| 队列瞬时观测 | 快速任务下 `/health.queue_depth` 可能为 0 | 以 `queue_position` 和 429 为准；如需精确监控加累计指标 |
| 历史预算 | 按 `num_ctx` 自动计算，32K 下约 18 条；超出预算的早期轮次不可召回 | 需要更长记忆时提高 `history_budget_ratio` / `num_ctx`，或另做检索层 |
| 16G 单卡 | 未在真实 16G A 卡验证；单卡 20G 近似通过 | 上线前在目标卡跑 `audit_single_gpu.py` + 32K 长上下文 |
| `vram_mb` 估算 | 权重+KV 的估算值，偏差过大会误判可用性 | 按目标机实测校准，不要只依赖默认粗估 |
| 无流式输出 | 所有接口非流式，长任务只能轮询 | 需要时加 SSE；当前按“链路通畅”未做 |
| 无鉴权/速率限制 | 默认绑 `127.0.0.1`，对外需反代 + 鉴权 | 内网使用；公网前必须加 |
| 日志/DB 增长 | 日志追加、任务与会话永久保留，无自动轮转/清理 | 生产部署加 logrotate 与定期清理 `data/state.db` |
| 长稳压测 | 未做小时级显存泄漏/长稳测试 | 上线前跑 2–4 小时循环任务 |
| GitHub runner | 未注册 self-hosted runner（无仓库地址/token）；集成脚本已在目标机手动跑通 | 推送到 GitHub 后给 GPU 机打 `gpu` 标签并注册 |

## 8. CI 结构与运行方式

```
.github/workflows/ci.yml
├── static（ubuntu-latest, Python 3.10）
│   └── tests/ci_static.sh
│       ├── compileall / bash -n
│       ├── check_config.py      # 13 项配置解析 + 4 类错误配置 fail-fast
│       ├── check_portability.py # 禁止模型名/显卡型号/绝对路径写回代码
│       ├── check_docs.py        # 文档端点与 settings 段一致性、旧项目名残留
│       └── ruff（E9/F63/F7/F82）
└── integration（self-hosted, linux, gpu）
    └── tests/ci_integration.sh
        ├── 前置检查（GPU/模型/运行时）
        ├── 启动（独立端口 18110/18120/18121，独立 DATA_DIR/PID/日志）
        ├── smoke → 单档矩阵 → API 审计 → 长上下文 → 故障注入 → 重启/队列 → 稳定性
        └── trap 自动停服
```

本地等价命令：

```bash
# 静态层（任意机器）
bash tests/ci_static.sh

# 集成层（有 GPU、已部署模型与 pylib 的机器）
bash tests/ci_integration.sh

# 仅手动跑三类审计（服务已启动）
BASE=http://127.0.0.1:8100 ./tests/audit_runner.sh
```

自托管 runner 要求：GPU 可用、`llama-server` 与模型已就位、`pylib/` 已安装、runner 标签含 `gpu`。集成层会独占 GPU 端口，**不要与生产实例共用同一组端口**。

## 9. 文档修订

本轮同步修订：

| 文档 | 修订点 |
|---|---|
| `README.md` | 工作区改为占位；启动加 `--wait`；补错误码/限额/CI 命令；文档索引加本报告 |
| `OPERATIONS.md` | 删 `budget`；补 `tier_order`/`max_input_chars`/`queue_maxsize`/`n_gpu_layers`/输出限额；补重启恢复/前台自愈/413/429 排障；补 CI 与审计命令；安全节更新“已有输入上限” |
| `PROJECT_INTRO.md` | 架构补自愈/恢复/限额；配置表更新；接口表补错误码；已知限制更新 ROCm/模型质量/日志轮转 |
| `TEST_REPORT.md` | 声明本轮审计数据见本报告；测试清单更新 |
| `AUDIT_REPORT.md` | 本文件 |

## 10. 结论

1. **核心链路真实可用**：简单直答、中/重档异步、进度应答、结果原样递回、会话、并发、队列、上下文恒定均通过真机验证。
2. **可靠性达到可长期运行的门槛**：前台/后台进程崩溃可自愈，停服重启不丢状态且不卡死，队列有上限与明确 429。
3. **可移植性从“文档声称”变为“静态检查强制”**：代码与脚本中已无模型名、显卡型号、设备绝对路径；槽位与档位名可配置。
4. **CI 可执行**：静态层与 GPU 集成层均在本轮实跑通过；接入 GitHub 只差注册 self-hosted runner。
5. **下一步优先级**：ROCm 单卡实测 > 换更强 9B/35B 模型 > 日志/DB 轮转 > 鉴权/流式。

## 11. 交付物与设备状态

- 代码：`app/`（config/gpu/llama/router/store/main/embed）
- 配置：`settings.yaml`
- 脚本：`run.sh`、`tests/run_matrix.sh`
- 测试：`test_smoke.py`、`test_slot_switch.py`、`test_stability.py`、`test_matrix.py`、`audit_api.py`、`audit_long_context.py`、`audit_single_gpu.py`、`audit_faults.py`、`audit_restart.py`
- CI：`tests/ci_static.sh`、`tests/ci_integration.sh`、`tests/check_config.py`、`tests/check_portability.py`、`tests/check_docs.py`、`.github/workflows/ci.yml`
- 实测结果：`logs/audit_api.json`、`logs/audit_faults.json`、`logs/audit_restart.json`、`logs/matrix_*.json`、`logs/ci/matrix_*.json`

设备已恢复：本项目服务已停、显存已释放；未删除其它目录内容、未配置开机自启。

---

## 12. 补充：消息传递真机多轮测试（2026-11-06 04:19–04:27）

> 目的：验证路由之间消息传递的稳定性、格式统一性与模板一致性，三轮数据量递增。
> 环境：Ubuntu 22.04 / 双卡 RTX 3080 20G / CUDA / `num_ctx=32768` / 3B 前台 + 9B 中档 + 35B MoE 重档。

### 12.1 三轮结果

| 轮次 | 数据量 | 回合 | 总输入 | 直答 / 后台 | 后台耗时 | 历史增长 | 召回 |
|---|---|---|---|---|---|---|---|
| 1 | 小（8–27 字/轮） | 5 | 81 字 | 5 / 0 | — | 0→149 字，0→8 条 | 暗号准确 |
| 2 | 中（30–615 字/轮） | 5 | 1,748 字 | 3 / 2（mid + heavy） | 9.69s / 18.87s | 0→4,011 字，0→8 条 | 3/3 准确 |
| 3 | 大（3,518–4,516 字/轮） | 5 | 12,082 字 | 3 / 2（heavy） | 11.72s / 14.22s | 0→11,224 字，0→6 条 | 截断后误答 |

### 12.2 稳定性结论

- 15 个回合全部 HTTP 200，无 500、无超时、无连接失败。
- 4 个后台任务全部 `completed`；`final_reply` 与 `instant_reply` 不同，确认结果原样递回。
- 历史角色严格交替（0→2→4→6→8 条），未触发 chat template 异常。
- 最大 `estimated_total_chars` 13,210 / 32,768，无上下文溢出。
- 小数据量延迟 0.16–0.64s；长输入前台 prefill 1.3–2.6s。

### 12.3 本次发现并修复的缺陷

| 问题 | 真机表现 | 处理 |
|---|---|---|
| 历史截断完全静默 | 第 3 轮第 4 回合历史从 8,100 字降到 4 条，模型不知道存在更早轮次 | `context_usage.history_truncated` 标记 + `prompts.HISTORY_TRUNCATED_NOTE` 系统提示 |
| 3B 忽略截断提示 | 三次强化提示后，仍把第二轮标记当作第一轮答案 | 记为模型能力边界，文档如实标注，不再声称“模型会明确说无记录” |

### 12.4 已知限制（真机确认）

`history_truncated=true` 时，轻量前台仍可能误答更早轮次的内容。**不要依赖截断后的早期召回**；需要精确回溯时应由客户端保留原始记录。要根治只有两条路：换更强的前台模型，或引入检索层（当前明确不做）。

### 12.5 复现

```bash
cd <工作区>
python3 tests/msg_rounds_test.py 1   # 小数据量：短对话，全部直答
python3 tests/msg_rounds_test.py 2   # 中等：300–900 字/轮，前台 + 中档 + 重档
python3 tests/msg_rounds_test.py 3   # 大：3,000–5,000 字/轮，压上下文预算
# 结果写入 logs/msgtest_round<N>.json，含每轮字符数、模式、槽位、历史预算与截断标记
```

---

## 13. 补充：OpenAI 兼容层真机测试（2026-11-08 18:30–18:40）

### 13.1 变更范围

新增 `/v1/chat/completions` 与 `/v1/models`，把「即时回复 + 后台任务」的自定义协议适配成标准 OpenAI 协议；`/chat` 保持原样。两条入口共用 `app/main.py::_pipeline()`，路由、上下文预算、降级与结果原样递回完全一致。

### 13.2 测试环境

同一台设备（`liuda-API`，Ubuntu 22.04 / Python 3.10.12），双卡 RTX 3080 20G，CUDA，`num_ctx=32768`，`openai.default_mode=final`、`wait_timeout_s=120`。服务冷启动后前台 `ready`，测试结束已停服、端口释放。

### 13.3 结果：36/36 通过

| 组 | 覆盖 | 结果 |
|---|---|---|
| 模型探测 | `/v1/models` 的 `object=list`、`explore`、`explore-fast`、`explore-<tier>` | 4/4 |
| direct | `object=chat.completion`、content 非空、`process_mode=direct`、`task_status=completed`、usage 为正 | 7/7 |
| instant | 立即返回（非阻塞）、`process_mode=async`、给出 `task_id`、后台最终 `completed`、即时回复不等于后台结果 | 7/7 |
| final | 同步等到 `completed`、tier 命中、**content 与自身 task 的 `final_reply` 逐字一致**、content 与即时回复不同 | 6/6 |
| 流式（direct） | `text/event-stream`、首帧 `role=assistant`、正文增量、`finish_reason=stop`、`[DONE]` | 5/5 |
| 流式（async） | 两段内容、正常收尾、期间持续有数据帧 | 3/3 |
| 错误路径 | 空 messages `400` + OpenAI 错误体、非法 `explore_mode` `400` + `invalid_mode`、超长输入 `413` + `context_length_exceeded` | 3/3 |
| 合计 | | **36/36** |

### 13.4 关键验证

- **结果原样递回可验证**：`final` 模式的 `choices[0].message.content` 与该响应 `explore.task_id` 对应任务的 `final_reply` **逐字相等**（222 字对 222 字）。
- **instant 不偷塞后台结果**：`instant` 模式返回的是前台即时回复（440 字），后台结果（222 字）只落在 `/task_result`，两者内容不同。
- **超时回退实测**：临时把 `wait_timeout_s` 调到 3s，heavy 请求 5.09s 返回，`explore.fallback=timeout`、`task_status=processing`、content 等于即时回复、附 `hint`；该任务随后自行完成（`final_reply` 222 字），证明"超时不丢任务"。
- **`/chat` 未回归**：`tests/test_smoke.py` 11/11，`all_pass: true`；上下文恒定 `32768 → 32768`；槽位切换正常。

### 13.5 设备状态说明（非协议层问题）

本次 `moe` 槽位被 `vram_plan` 标记为 `available=false`：

```
gpu1: need 17333MB > usable 17243MB (free 19671MB, front 0MB)
```

差值仅 90MB，原因是启动瞬间 gpu1 的 free 比 2026-11-06 测试时少约 1GB（桌面/其它进程占用），导致 `heavy` 按设计降级到 `mid`。这正好验证了**降级路径可用**：响应里 `tier=heavy`、`chosen_slot=mid`，链路正常完成。

但这意味着本轮 heavy 任务实际由 9B 完成，**未覆盖 35B MoE**。需要跑重档的机器请按实际 free 校准 `slots.moe.vram_mb` 或 `split_ratio`，再用 `/config` 确认 `vram_plan.moe.available=true`。

### 13.6 复现

```bash
cd <工作区>
PYTHONPATH=$PWD/pylib:$PWD python3 tests/test_openai_api.py
# 结果写入 logs/openai_api_test.json，含逐项 check 与 passed/total
PYTHONPATH=$PWD/pylib:$PWD python3 tests/test_smoke.py   # /chat 回归
```

---

## 14. 补充：Responses API 真机测试（2026-11-08 18:52–19:00）

### 14.1 变更范围

新增 `POST /v1/responses` 与 `GET /v1/responses/{id}`：支持 `input`（字符串或消息数组）、`instructions`、`store`、`previous_response_id`、细分 SSE 事件（`event:` + `data:`，无 `[DONE]`）。`/v1/chat/completions` 与 `/chat` 未改动，三条入口共用 `_pipeline()`。

### 14.2 结果：38/38 通过

| 组 | 覆盖 | 结果 |
|---|---|---|
| 非流式 direct | `object=response`、`status=completed`、`output[0]` 为 message + output_text、`process_mode=direct`、usage 字段 | 7/7 |
| final | `status=completed`、`process_mode=async`、tier 命中、正文与自身 task 的 `final_reply` 逐字一致 | 6/6 |
| instant | 立即返回、给出 `task_id`、后台任务最终 `completed` | 5/5 |
| 流式 | 有 `event:` 行、以 `response.created` 开头、delta→done→completed 顺序、拼接非空、无 `[DONE]` | 8/8 |
| previous_response_id | 第一轮返回 `resp_` id、第二轮 `history_messages>=2`、召回暗号 `RESP-7Q2K` | 5/5 |
| GET 检索 | `200` 且内容与首次返回逐字一致 | 2/2 |
| store=false | 请求成功、事后 GET 返回 `404` | 2/2 |
| 错误路径 | 空 `input` → `400`；未知 `previous_response_id` → `404` + `previous_response_not_found` | 2/2 |
| 合计 | | **38/38** |

### 14.3 回归

- `/v1/chat/completions`：**36/36**（上一轮全套，未受影响）。
- `/chat`：`test_smoke.py` **11/11**，`all_pass: true`。
- 旧库兼容：`responses` 表在既有 `data/state.db` 上通过 `CREATE TABLE IF NOT EXISTS` 创建，`messages` / `tasks` 未受影响；重复 `init()` 幂等。

### 14.4 关键验证

- 流式 `response.output_text.done` 的文本与所有 delta 拼接**完全一致**，且流**不以 `[DONE]` 结尾**——这是 Responses 与 chat/completions 最容易做错的一处。
- `previous_response_id` 续接确实带上了服务端历史（`explore.history_messages=2`），3B 前台在第二轮正确召回 `RESP-7Q2K`。
- `store=false` 的请求不写入 `responses` 表，事后按 id 检索返回 `404`，语义与 OpenAI 一致。

### 14.5 超时语义（设计，未单独计时）

Responses 复用第 13.4 节的同一套等待逻辑（`openai.wait_timeout_s`）。超时分支返回 `status="incomplete"` + `incomplete_details.reason="timeout"`，并保留 `explore.task_id`，**不会谎称 `completed`**。本次未对该分支单独做秒级计时验证。

### 14.6 明确未实现

工具调用（`tools` / `tool_choice`）、结构化输出（`text.format`）、`reasoning`、`truncation`、`background` 模式、`input_image` / `input_file` 等非文本模态。字段会被接受但不生效，或按纯文本处理。

### 14.7 复现

```bash
cd <工作区>
PYTHONPATH=$PWD/pylib:$PWD python3 tests/test_responses_api.py
# 结果写入 logs/responses_api_test.json
PYTHONPATH=$PWD/pylib:$PWD python3 tests/test_openai_api.py   # chat/completions 回归
PYTHONPATH=$PWD/pylib:$PWD python3 tests/test_smoke.py        # /chat 回归
```

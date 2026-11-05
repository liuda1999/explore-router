# explore 智能本地路由 — 运维手册

> 配置驱动版 v2.2.0 / 2026-11-06 / 工作区：`<部署目录>`（脚本按自身位置定位，不写死绝对路径）

## 文档导航

| 文档 | 内容 |
|---|---|
| `README.md` | 双语总入口、快速开始 |
| `PROJECT_INTRO.md` / `PROJECT_INTRO.en.md` | 完整项目介绍（中 / 英） |
| `TECH_STACK.md` | 技术栈、组件选型理由、兼容矩阵 |
| `DEPLOYMENT.md` | 从零部署、启动流程、验收清单、CI 接入 |
| 本文件 | 日常运维：配置字段全表、换模型 / 换设备、监控、排障、回滚 |
| `AUDIT_REPORT.md` / `TEST_REPORT.md` | 真机审计 / 特定设备的历史实测 |
| `docs/PROJECT_PLAN.md` | 历史开发记录（需求演进，非当前约束） |

> 首次部署请先看 `DEPLOYMENT.md`；本手册假设服务已经跑起来。

## 1. 环境与支持范围

项目**不绑定具体硬件**。运行时探测 GPU 后端（CUDA / ROCm）、卡数与显存，再按 `settings.yaml` 分配槽位。

| 项 | 说明 |
|---|---|
| 支持的后端 | CUDA（NVIDIA）、ROCm（AMD）、CPU（探测不到 GPU 时） |
| 支持的卡数 | 单卡、多卡均可；多卡自动决定是否分层 |
| 模型运行时 | 默认设备自带的 `llama-server`（llama.cpp，GGUF）；路径可配 |
| Python | 3.10+；依赖版本固定在 `requirements.txt`（目标机实测：fastapi 0.162.0 / uvicorn 0.54.0 / httpx 0.28.1 / PyYAML 6.0.3 / numpy 2.2.6 / onnxruntime 1.23.2 / tokenizers 0.23.3 / websockets 16.1.1） |
| 依赖安装 | `pip --target <工作区>/pylib`，不动系统、不需要 venv |
| 端口 | 默认 8100 / 18100 / 18101，全部可配 |

> 历史测试环境是双卡 NVIDIA + CUDA 11.5；那只是**一次实测环境**，不是运行要求。单张 AMD 卡同样适用，见第 6 节。
>
> 组件选型理由、固定版本与兼容矩阵见 `TECH_STACK.md`；依赖安装与从零启动的完整步骤见 `DEPLOYMENT.md`。

## 2. 目录结构

```
explore-router/
├── settings.yaml          ★ 唯一配置源：槽位 / 模型 / 硬件 / 上下文 / 端口
├── app/
│   ├── config.py          加载 settings.yaml + 环境变量覆盖 + 设备自适应
│   ├── gpu.py             CUDA / ROCm / CPU 探测
│   ├── llama.py           运行时进程管理（不含硬件常量）
│   ├── embed.py           ONNX embedding
│   ├── store.py           SQLite 任务状态与会话历史
│   ├── router.py          分级 + 互斥槽位（按 key 操作）
│   ├── prompts.py         统一消息模板（任务包 / 前缀 / 降级文案）
│   └── main.py            FastAPI 接口
├── models/                GGUF 与 embedding（路径在 settings.yaml 中写）
├── pylib/                 Python 依赖
├── data/                  state.db、router.pid
├── logs/                  api.log / <slot>.log / matrix_*.json / audit_*.json
├── tests/                 回归 + 审计 + CI 脚本
├── docs/                  历史开发记录（PROJECT_PLAN.md）
├── run.sh                 幂等启停（路径按脚本位置，端口读配置）
├── requirements.txt       固定版本
├── .github/workflows/     CI 定义
└── *.md                   文档（README / PROJECT_INTRO / PROJECT_INTRO.en /
                           TECH_STACK / DEPLOYMENT / OPERATIONS /
                           AUDIT_REPORT / TEST_REPORT）
```

## 3. 部署（任意一台新设备）

> **完整部署与启动流程见 `DEPLOYMENT.md`**：前置条件、逐步命令、三种硬件场景、首次启动验收清单、
> 启动失败速查、升级回滚与 CI 接入。本节只保留最短路径。

```bash
cd <工作区>                              # 脚本按自身位置定位，路径可任意
mkdir -p models pylib data logs
python3 -m pip install --target ./pylib -r requirements.txt

# 放入 GGUF / ONNX 模型，然后按第 4 节编辑 settings.yaml
./run.sh start --wait
curl -s http://127.0.0.1:8100/config    # ★ 确认解析出的后端、卡数、槽位、N
```

**不需要**改任何 Python 代码。若 `python3-venv` 不可用，用 `--target` 即可，不要装系统级服务。
换模型 / 换设备后必做：`/config` 确认生效值 → `tests/quick_check.py` → 按设备能力跑 `tests/run_matrix.sh`。

## 4. `settings.yaml` 字段说明

| 段 / 字段 | 含义 | 改动影响 |
|---|---|---|
| `runtime.server_bin` / `lib_dir` | 运行时二进制与动态库目录 | 换运行时或换机器路径 |
| `runtime.gpu_backend` | `auto` / `cuda` / `rocm` / `cpu` | 强制指定后端时用 |
| `runtime.n_gpu_layers` | 卸载到 GPU 的层数（默认 99） | CPU/部分卸载场景可调小 |
| `runtime.max_output_tokens` | 后台单次生成上限（默认 1024） | 长文任务可调大，注意耗时 |
| `runtime.queue_maxsize` | 后台排队上限（默认 32） | 满时 `/chat` 返回 429；0 表示不限制 |
| `runtime.ports.{api,front,slot}` | 三个端口 | 避免端口冲突 |
| `runtime.startup_timeout_s` / `request_timeout_s` | 超时 | 慢盘/大模型可调大 |
| `context.num_ctx` | `auto` 或整数 | `auto` 按显存查 `auto_table` |
| `context.auto_table` | 总显存 → 默认 N 的启发式表 | **最低 32K**；更大上下文需实测后加表项或用 `NUM_CTX` 覆盖 |
| `context.kv_cache` | KV 精度（如 `q8_0`） | 只影响显存，不改变 `num_ctx` |
| `embedding.onnx` / `tokenizer` | 嵌入模型路径 | 换嵌入模型 |
| `budget.reserve_system_mb` | 系统/驱动预留显存，按可见卡数分摊 | 小显存设备可调小，但不建议为 0 |
| `budget.headroom_ratio` | 显存安全余量（默认 0.10） | 装不下时宁可标记不可用，也不 OOM |
| `slots.<key>.model` | 模型文件（相对工作区） | **换模型只改这里** |
| `slots.<key>.quant` / `weight_mb` / `vram_mb` | 量化、权重估算、权重+KV 总显存估算 | `weight_mb` 参与多卡自动分层扣减；`vram_mb` 用于显存可行性判断，**16G 单卡务必按实测填**；不填则按 `weight_mb*1.35+512` 粗估 |
| `slots.<key>.placement` | `auto` / `spread` / `explicit` | 设备分配策略 |
| `slots.<key>.split_ratio` | 可选，整数比例如 `[1, 2]` | 多卡分层比例；不写则按「空闲显存 − 前台预估占用」自动算。自动比例与实测偏差较大时可显式指定 |
| `slots.<key>.resident` | 是否永久常驻前台 | **必须有且仅有一个 `true`**，否则启动失败 |
| `routing.tier_order` | 三个档位的名称（默认 `simple/mid/heavy`） | 可改名（如 `quick/standard/deep`），需与 `tier_to_slot` 同步 |
| `routing.tier_to_slot` | tier → 槽位 key | 增删槽位时同步改；指向不存在的槽位会启动失败 |
| `routing.default_slot` | 未知 tier 的兜底槽位 | 必须是已存在的槽位 |
| `routing.mid_min_chars` / `heavy_min_chars` / `complex_words` | 分级规则 | 需满足 `0 < mid < heavy`，否则启动失败 |
| `routing.max_input_chars` | 单次输入字符上限（`auto` = `num_ctx*1.5`） | 超限返回 413；防止超大请求 |
| `frontend.max_tokens` | 即时回复上限（默认 256） | 前台回复过长可调 |
| `frontend.history_budget_ratio` | 历史预算占「可用上下文 − 当前输入」的比例（默认 0.45） | **自动阈值**：短输入多带历史，长输入自动收紧 |
| `frontend.history_min_chars` | 历史最小字符预算（默认 2048），输入上限会相应扣减 | 不建议低于 512 |
| `frontend.history_max_messages` | 历史条数上限（默认 40） | 防止极端多轮拖慢 prefill |
| `frontend.context_safety_tokens` | token 估算误差与模板开销预留（默认 256） | 小上下文设备可保留默认 |
| `frontend.restart_cooldown_s` | 前台异常重启冷却（默认 30s） | 防止崩溃循环 |
| `frontend.progress_window_s` | 进度提问关联的任务时间窗（默认 1800s） | 避免把很久前的任务当进度 |
| `frontend.system_prompt` | 前台提示词 | 与模型无关 |

**环境变量覆盖**（优先级高于文件）：`SETTINGS_FILE`、`NUM_CTX`、`KV_CACHE`、`GPU_BACKEND`、`N_GPU_LAYERS`、`MAX_OUTPUT_TOKENS`、`QUEUE_MAXSIZE`、`FRONT_MAX_TOKENS`、`API_PORT`、`FRONT_PORT`、`SLOT_PORT`、`LLAMA_SERVER`、`LLAMA_LIB_DIR`、`STARTUP_TIMEOUT_S`、`REQUEST_TIMEOUT_S`、`DATA_DIR`、`LOG_DIR`、`PID_FILE`、`LOG`。

`GET /config` 会返回实际生效值，包括 `num_ctx_source`（是查表得来、文件指定还是环境变量覆盖）、每个槽位的 `devices` 与 `split_ratio`。**改完配置先看这个接口。**

## 5. 换模型（不改代码）

编辑 `settings.yaml` 对应槽位，例如把前台换成别的 3B/4B 小量化模型：

```yaml
slots:
  front:
    model: models/another-4b-q4.gguf
    quant: Q4_K_M
    weight_mb: 2600
    placement: auto
    resident: true
```

中档 / 重档同理：改 `model` 即可，重档可以是 MoE 也可以是稠密模型。若模型变大显存吃紧，可：
- 把该槽位 `placement` 设为 `spread`（多卡时跨卡分层，比例按空闲显存自动算）；
- 调 `context.kv_cache` 为更省显存的精度；
- 或调小 `context.num_ctx` / 改 `auto_table`。

**增减槽位**：在 `slots` 增删条目并同步维护 `routing.tier_to_slot`；代码遍历配置，不假设槽位数量。

改完：
```bash
./run.sh stop && ./run.sh start
curl -s http://127.0.0.1:8100/config
./tests/run_matrix.sh        # 按设备能力设 CTX_MATRIX
```

## 6. 换设备 / 硬件适配

### 单张 AMD 卡（示例）

```yaml
runtime:
  gpu_backend: auto            # 探测到 rocm-smi 即使用 ROCm
  ports: {api: 8100, front: 18100, slot: 18101}
context:
  num_ctx: auto                # 按该卡显存查 auto_table
slots:
  front: {model: models/small-4b-q4.gguf, placement: auto, resident: true, ...}
  mid:   {model: models/mid-8b-q4.gguf,  placement: auto, ...}
  moe:   {model: models/heavy-q4.gguf,   placement: auto, ...}   # 单卡不用 spread
```

启动时 `config.py` 会：`gpu.snapshot()` → 后端 `rocm`、卡数 1 → 所有槽位 `devices=[0]` → 不启用 `-sm/-ts` → 进程环境用 `HIP_VISIBLE_DEVICES` / `ROCR_VISIBLE_DEVICES`。**无需改代码。**

### 多卡设备

- 前台常驻第一张卡；其它单卡槽位放最后一张卡，避开前台；
- `placement: spread` 的槽位跨所有卡，`split_ratio` 按各卡空闲显存自动计算；
- 若某卡被别的服务占用，比例会自动偏向空闲卡。

### 16G 单卡 / 小显存（重点）

启动时 `vram_plan` 会按**进程实际可见 GPU**（尊重 `CUDA_VISIBLE_DEVICES` / `HIP_VISIBLE_DEVICES`）的
`free_mb`、前台常驻占用和 `budget` 余量，判断每个槽位能否装下：

- 装不下的槽位标 `available: false`，`/config` 给出 `reason`（需要多少、可用多少）；
- `heavy` 任务自动降级到 `routing.default_slot`（通常是 mid），`selection_reason` 与 `release_info` 会写明“降级”；
- **不会**强行加载超预算模型导致 OOM；前台槽位装不下时服务启动直接失败并报原因。

建议的 16G 单卡配置：

```yaml
slots:
  front: {model: models/3b-q4.gguf, vram_mb: 3200, resident: true}
  mid:   {model: models/8b-q4.gguf, vram_mb: 6800}
  moe:   {model: models/14b-q4.gguf, vram_mb: 11500}   # 或直接移除重档
```

35B MoE（`vram_mb≈26000`）在 16G 下必然不可用；若保留该槽位，heavy 任务会走 mid。
换模型/量化后，把目标机实测的“权重 + KV”写进 `slots.<key>.vram_mb`；不填则用 `weight_mb*1.35+512` 粗估。

单卡近似验证（在双卡机上限制为一张 20G 卡）：

```bash
CUDA_VISIBLE_DEVICES=0 DATA_DIR=$PWD/data/single ./run.sh start --wait
curl -s http://127.0.0.1:8100/config | python3 -m json.tool | grep -A6 vram_plan
python3 tests/audit_single_gpu.py
```

### 校准步骤

1. `curl /config` 看探测结果是否符合实际（后端、卡数、`num_ctx_source`）；
2. 用 `CTX_MATRIX="8192 16384 32768" ./tests/run_matrix.sh` 选适合该设备的档位；
3. 若某档 OOM，调 `auto_table` / `kv_cache` / `placement`，不要改代码；
4. 把最终可用档位写回 `settings.yaml` 或记为部署参数。

## 7. 启停

```bash
cd <工作区>
./run.sh start --wait   # 幂等；--wait 等到前台就绪；写 data/router.pid；不设开机自启
./run.sh restart --wait
./run.sh status
./run.sh health
./run.sh stop            # 只停本项目 PID 与配置里的 front/slot 端口，绝不全局 pkill
```

启动会拉起 `resident: true` 的前台槽位；后台槽位**懒加载**，第一个复杂任务时才启动。
`run.sh` **不再预设 `NUM_CTX` 默认值**，完全交给 `settings.yaml`；部署期可用 `NUM_CTX=65536 ./run.sh start` 临时覆盖。

**恢复语义**：启动时把上次中断的 `processing` 任务标记为 `failed`（错误信息含“服务重启，任务中断”），`pending` 任务重新入队；不会永久卡在 pending。
**自愈语义**：前台模型进程异常时，下一次 `/chat` 会自动重启前台（默认 30s 冷却，实测约 3.3s 恢复）；后台进程异常时，下一次同档任务由 `SlotManager.ensure()` 重启。

## 8. 切换上下文档位

`num_ctx` 是运行期常量，切换需重启：

```bash
NUM_CTX=65536 ./run.sh stop && NUM_CTX=65536 ./run.sh start
curl -s http://127.0.0.1:8100/config | python3 -c "import json,sys; print(json.load(sys.stdin)['num_ctx'])"
```

整套矩阵（档位可按设备改）：

```bash
CTX_MATRIX="8192 16384 32768" ./tests/run_matrix.sh
cat logs/matrix_32768.json
```

> 历史报告里的 32K/64K/96K 与对应显存、耗时，是某台双卡设备的实测，**换设备后请重新校准**，不要照搬。

## 9. 显存预检

`run.sh start` 会先按 `settings.yaml` 的 `vram_mb` / `budget` 预检前台槽位显存，装不下时直接报错退出，
不会加载到一半 OOM。运行中可通过 `/config` 的 `vram_plan` 查看每个槽位的估算与可用性；
调整模型/量化或 `vram_mb` 后重启即可。

## 10. 监控

```bash
curl -s http://127.0.0.1:8100/config     # ★ 生效配置、后端、卡数、N 来源、槽位设备
curl -s http://127.0.0.1:8100/health    # 前台、槽位、队列、embedding、显存
curl -s http://127.0.0.1:8100/gpu
curl -s "http://127.0.0.1:8100/tasks?limit=20"
tail -f logs/api.log logs/<slot>.log
nvidia-smi / rocm-smi                    # 按实际后端
```

重点看：`slot.current`、`slot.switches`（同档任务不应增加）、`slot.last_error`、`queue_depth`（空闲时应为 0）、`embedding.dim`（启动自检后即为 1024，若为 0 看 `embedding.error`）、`vram_plan`（每个槽位是否装得下、不可用的原因）。

## 11. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动报 `settings.yaml not found` | 配置文件缺失或路径不对 | 用 `SETTINGS_FILE` 指定，或放回工作区根目录 |
| `必须有一个 resident=true 的前台槽位` | slots 配置缺前台 | 给前台槽位加 `resident: true` |
| `/config` 显示 `gpu_count=0` / backend=cpu | 探测不到 GPU 或后端探测工具缺失 | 装 `nvidia-smi` / `rocm-smi`；或显式 `GPU_BACKEND`；确认运行时支持该后端 |
| 模型加载失败 | `slots.<key>.model` 路径错 / 架构不被运行时支持 | 查 `logs/<slot>.log`，改路径或换模型 |
| 大上下文 OOM | 常驻前台 KV 挤占显存 | 调 `context.kv_cache`、`auto_table`，或把重档 `placement` 设 `spread`；不要改代码 |
| 后台结果为空 | thinking 模型把正文写入 `reasoning_content` | 调用层已用 `enable_thinking=false` + `/completion` 兜底，勿删 |
| `embedding.dim=0` | ONNX 路径/参数问题 | 看 `/health` 的 `embedding.error`，核对 `embedding.*` 路径 |
| 槽位切换失败后状态不一致 | 已修：失败清空 `current`/`server` 并写 `last_error` | 看 `/health` 的 `slot.last_error` |
| 端口被占用 | 与已占用端口冲突 | 改 `runtime.ports`，不要 `pkill -9 python`；`run.sh start` 会先检查并明确报错 |
| uvicorn 报 `ServerProtocol` | 系统 `websockets` 太旧 | 按 `requirements.txt` 把 `websockets` 装进 `pylib` |
| `413 user_input 过长` | 输入超过 `routing.max_input_chars` | 调大配置，或拆分请求；这是防止超大请求撑爆 SQLite/模型 |
| `429 后台队列已满` | 并发复杂任务超过 `runtime.queue_maxsize` | 等队列排空或调大上限；简单任务不受影响 |
| 重启后任务变 `failed` | 重启时任务正在处理，已标记中断 | 看 `error_msg` 是否含“服务重启”；重新提交即可，不会永久卡死 |
| `/health.frontend=down` | 前台进程崩溃 | 下一次 `/chat` 会自动重启；若持续 down 看 `logs/front.log` 与 `slot.last_error` |
| 日志/数据库持续增长 | 无自动轮转/清理 | 生产环境加 logrotate，定期归档或清理 `data/state.db` |
| `/config.vram_plan.<slot>.available=false` | 显存估算装不下该槽位 | 换更小模型/量化、调 `vram_mb`，或让 heavy 自动降级；**不要强行加载** |
| heavy 任务 `chosen_model` 是 mid | 重档显存不足，自动降级 | 正常保护；看 `selection_reason` / `release_info` 的“降级”说明 |
| 多轮对话越来越慢 | 历史预算自动变大，prefill 变长 | 调小 `frontend.history_budget_ratio` / `history_max_messages`，或提高 `num_ctx` |
| `ERROR: 前台槽位显存不足` | 显存不足或模型/量化过大 | 释放显存、换更小模型/量化，或调低 `vram_mb`；不要强行启动 |

## 12. 测试与回归

测试脚本**从 `/config` 动态读取端口、槽位名、档位名**，换模型/换设备后无需修改：

```bash
cd <工作区>
export PYTHONPATH=$PWD/pylib:$PWD
python3 tests/test_smoke.py        # 基础链路（后端不限 CUDA/ROCm）
python3 tests/test_slot_switch.py  # 槽位切换 / 上下文恒定
python3 tests/test_stability.py    # 连续请求，N 从 /health 读
python3 tests/quick_check.py       # 主路快速检查（约 1 分钟）

# 真机多轮消息传递（三轮不同数据量；结果写 logs/msgtest_round<N>.json）
python3 tests/msg_rounds_test.py 1   # 小数据量：短对话，全部前台直答
python3 tests/msg_rounds_test.py 2   # 中等：300-900 字/轮，前台 + 中档 + 重档
python3 tests/msg_rounds_test.py 3   # 大数据：3000-5000 字/轮，压上下文预算

CTX_MATRIX="8192 16384 32768" ./tests/run_matrix.sh

# 全量真实审计（服务已启动；含 API/并发/长上下文/故障注入/重启恢复）
BASE=http://127.0.0.1:8100 ./tests/audit_runner.sh

# 单卡/小显存专项（双卡机上用 CUDA_VISIBLE_DEVICES=0 近似）
python3 tests/audit_single_gpu.py

# CI
bash tests/ci_static.sh            # 静态层：任意机器
bash tests/ci_integration.sh       # GPU 集成层：自托管 GPU 机器，独立端口/数据目录
```

矩阵结果写入 `logs/matrix_<ctx>.json`（含断言、速度、显存、生效配置、日志 `n_ctx_slot`）。
每次改 `settings.yaml`、换模型、换设备、调 KV 参数后都应重跑。
本机实测结果见 `AUDIT_REPORT.md`；CI 结构与 runner 要求见该报告第 8 节。

## 13. 备份与回滚

```bash
tar -czf ~/explore-router-backup.tgz -C <工作区> \
  app tests settings.yaml run.sh requirements.txt .github docs \
  README.md PROJECT_INTRO.md PROJECT_INTRO.en.md TECH_STACK.md DEPLOYMENT.md \
  OPERATIONS.md TEST_REPORT.md AUDIT_REPORT.md
```

- 配置回滚：恢复 `settings.yaml` 后 `./run.sh stop && ./run.sh start`。
- 代码回滚：恢复 `app/` 后重启。
- 设备回滚：`./run.sh stop`，设备回到无本项目进程的状态。
- `data/state.db` 可删重建（仅任务与会话记录）；**不要删 `models/`**。

## 14. 安全与偏好

- API 默认绑 `127.0.0.1`；对外暴露需加鉴权与 TLS 反向代理。
- 当前无鉴权、无速率限制；已有输入长度上限（413）与后台队列上限（429），仍仅适用于内网。
- 日志与 `data/state.db` 无自动轮转/清理，长期运行需外部 logrotate 与定期归档。
- **禁止** `systemctl enable`、开机自启、后台守护常驻；服务只在主动 `start` 后存在。
- **禁止** `pkill -9 python` / 清运行时缓存这类全局操作。
- 凭据不要写入工作区、不要提交 git；`settings.yaml` 不含密钥。

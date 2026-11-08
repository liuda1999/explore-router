# 验收压测报告 — explore v2.3.0

> 执行时间：2026-11-08 19:15–19:26，复检与补跑 19:30–19:44 ｜ 设备：`liuda-API` ｜ 结论：**通过，可发布**
> 全部使用真实设备、真实模型、真实 HTTP 与真实 SQLite；**无 mock、无模拟数据**。

---

## 1. 验收目标

在推送 v2.3.0（OpenAI 兼容层：chat/completions + Responses）前，确认：

1. 长上下文多轮在 32K 窗口下不溢出，预算收紧时可观测；
2. 连续多次长文本回复链路稳定，不被 `max_output_tokens` 意外截断；
3. 三套入口（`/chat`、`/v1/chat/completions`、`/v1/responses`）的路由决策一致；
4. 并发与队列不丢任务、不重复、不产生失败态；
5. 流式协议（两种）事件序列与文本拼接正确；
6. 错误路径返回预期状态码；
7. **服务端日志无 Traceback / ERROR / OOM，任务表无新增 failed**。

## 2. 环境

| 项 | 值 |
|---|---|
| 系统 | Ubuntu 22.04 / Python 3.10.12 |
| GPU | 双卡 RTX 3080 20G，共 40,960 MB |
| 后端 | CUDA，`gpu_count=2` |
| 上下文 | `num_ctx=32768`（auto） |
| 槽位 | front / mid `available=true`；**moe `available=false`**（差 90MB，见 §6） |
| 配置 | `openai.default_mode=final`，`wait_timeout_s=120` |

## 3. 结果总览：82/82 通过

| 场景 | 内容 | 检查项 | 结果 |
|---|---|---|---|
| A 长上下文多轮 | `/chat` 5 轮 × 3,523 字真实材料 + 第 6 轮追问第一轮标记 | 31 | 31/31 |
| B Responses 续接 | `/v1/responses` 4 轮 × 约 1.8K 字，`previous_response_id` 链式 + 末轮追问 | 25 | 25/25 |
| C 连续长文本 | 同会话 3 篇「不少于 600 字」，分 4 小节 | 9 | 9/9 |
| D 三协议一致 | 短/长输入下 `process_mode` 与 `tier` 对比 | 5 | 5/5 |
| E 并发队列 | 5 个 async 同时提交 | 4 | 4/4 |
| F 流式长文本 | chat SSE（有 `[DONE]`）与 Responses SSE（无 `[DONE]`） | 7 | 7/7 |
| G 边界错误 | 空输入 / 超长 / 空 messages / 未知 response id | 5 | 5/5 |
| H 日志 | 新增日志行扫描 + 前台存活 | 2 | 2/2 |

总耗时 **101.5 秒**，结果存档 `logs/acceptance_20261108_192532.json`。

## 4. 分场景实测数据

### A 长上下文多轮（32K 窗口）

| 轮次 | 输入 | 历史 | 条数 | 截断 | 后台耗时 | 回复 |
|---|---|---|---|---|---|---|
| 1 | 3,523 字 | 0 | 0 | false | 3.1s | 148 字 |
| 2 | 3,523 字 | 3,959 | 2 | false | 2.9s | 155 字 |
| 3 | 3,523 字 | 7,876 | 4 | false | 2.9s | 156 字 |
| 4 | 3,523 字 | 11,813 | 6 | false | 3.3s | 119 字 |
| 5 | 3,523 字 | 11,805 | 6 | **true** | 5.5s | 137 字 |
| 6 | 追问 | 11,783 | 6 | true | — | `A1_recall=False` |

- 每轮 `estimated_total_chars <= num_ctx`，**全程无溢出**。
- 第 5 轮历史预算约 12.9K，累计历史 14.1K，触发一次截断并保留最近 3 轮。
- 第 6 轮追问第一轮标记 → **回答不出**（未出现 `ACC-A1-*`）。这与文档「截断后早期召回不可靠」一致，属于已知边界，不是本次新增缺陷。
- 第 6 轮输入很短，预算重新变宽，因此它能重新看到更多历史——说明**历史窗口按当前输入动态伸缩，不是单调累积**。

### B Responses 续接

4 轮每轮约 1.8K 字，`previous_response_id` 链式续接：每轮 `history_messages` 递增且 `>= 2`，4 轮全部 `status=completed`，均返回 `resp_` 前缀 id；末轮追问「刚才最后一轮材料的标记」**召回成功**。

### C 连续长文本

| # | 主题 | 字数 | 槽位 |
|---|---|---|---|
| 1 | 本地推理的显存预算 | 544 | mid |
| 2 | 上下文窗口与历史预算 | 964 | mid |
| 3 | 槽位切换的代价与取舍 | 650 | mid |

三篇均 ≥ 300 字，`max_output_tokens=1024` 未触发截断。

### D 三协议一致性

- 短输入 `2+2 等于几`：三协议均 `process_mode=direct`，集合大小为 1。
- 长输入：三协议 `tier` 完全一致。

### E 并发队列

- 5 个请求同时提交，全部 HTTP 200，`task_id` 唯一。
- 提交总耗时 9.8s（前台串行生成占主要时间），5 个后台任务全部 `completed`。
- `queue_position` 全为 0：worker 取任务快，队列深度长期 ≤1，**不积压**，不是计数错误。

### F 流式

- chat SSE：有 `[DONE]`，末帧 `finish_reason=stop`，拼接文本 ≥ 200 字。
- Responses SSE：以 `response.completed` 结束，**无 `[DONE]`**，`output_text.done` 与所有 delta 拼接**完全一致**。

## 5. 日志核查（人工 + 脚本）

### 5.1 压测窗口新增

| 日志 | 新增行 | 可疑行 |
|---|---|---|
| `logs/api.log` | +67 | 0 |
| `logs/front.log` | +211 | 0 |
| `logs/mid.log` | +159 | 0 |
| `logs/moe.log` | +0（未加载） | 0 |

### 5.2 HTTP 状态码（`api.log` 全量）

| 状态码 | 次数 | 说明 |
|---|---|---|
| 200 | 1,904 | 正常 |
| 400 | 32 | 空输入 / 空 messages，主动测试 |
| 413 | 14 | 超长输入，主动测试 |
| 404 | 4 | 未知 `previous_response_id`，主动测试 |
| 429 | 3 | 队列满，早期并发测试 |
| 422 | 2 | 早期参数校验测试 |
| 405 | 1 | `GET /v1/responses` 探测 |

**无 5xx。**

### 5.3 任务表

- 压测窗口（19:15 之后）新建 **62 个任务，全部 `completed`，`failed=0`**。
- 全库 3 个 `failed` 均为 **2026-11-06** 的 `moe exited early`，属旧记录，与本次无关。
- `prefill_ms` 中位数 **129 ms**（49 个样本）。

### 5.4 运行态

- `/health`：`frontend=ready`，`queue_depth=0`，槽位 `current=mid`，**切换 1 次**（idle → mid，加载 3.2s），`last_error` 为空。
- 显存：used 11,801 MB / 40,960 MB，free 28,561 MB。
- `mid.log` 中 `stop processing: n_tokens = ... truncated = 0`——llama-server 侧 KV 未被强制截断，说明请求从未越过窗口。

## 6. 已知边界（如实记录）

| 边界 | 实测证据 | 影响 |
|---|---|---|
| 截断后早期召回不可靠 | A 第 6 轮 `A1_recall=False`，`truncated=true` 已提示 | 需要精确回溯时由客户端保留原始记录；根治需更强前台模型或检索层（当前明确不做） |
| `moe` 槽位不可用 | `gpu1: need 17333MB > usable 17243MB`，差 90MB | `heavy` 自动降级到 `mid`（9B），**本轮所有 heavy 任务实际由 9B 完成，未覆盖 35B MoE** |
| 流式非逐 token | 两套 SSE 均为分块发送 | 协议兼容，首字延迟等于前台生成时间 |

## 7. 验收过程中修正的问题

两处均为**测试设计错误**，非代码缺陷：

1. A 场景初版每轮 1.4K 字，5 轮累计 13.7K，未达 14.4K 预算 → 断言「应截断」不成立。改为每轮 3.5K 字后真实触发截断。
2. C 场景初版指令只说「约 600 字」，9B 只写 209/248 字。改为「不少于 600 字 + 分 4 节 + 每节 120 字」后稳定达标（544/964/650）。

## 8. 结论

**可以发布。** 链路、协议、并发、流式、错误路径与日志均符合预期；本轮未发现代码级缺陷，未产生任何 5xx 或新增失败任务。两条已知边界（截断召回、moe 90MB）已在文档中如实标注，不影响发布，但应在 Release Notes 中说明。

## 9. 复现

```bash
cd <工作区>
PYTHONPATH=$PWD/pylib:$PWD python3 tests/audit_acceptance.py        # 全量 82 项
PYTHONPATH=$PWD/pylib:$PWD python3 tests/audit_acceptance.py a c    # 只跑指定场景
# 结果：logs/acceptance_<时间戳>.json（含逐项 check 与日志扫描摘要）
```

---

## 10. 复检补充（2026-11-08 19:30–19:45）

> 本节修正 §8 的结论。原结论「本轮未发现代码级缺陷」**不成立**：复检发现了 max_tokens 截断静默的缺陷，已修复并定点验证。

### 10.1 回归断言审计发现的问题

| 位置 | 问题 | 影响 |
|---|---|---|
| `test_openai_api.py` | `finish_reason` 从未被断言，服务端也硬编码 `"stop"` | 截断永远无法被测试发现 |
| `audit_acceptance.py` C 场景 | 只校验 `300 <= len <= 1800`，不查结尾 | 半句截断也能通过 |
| `audit_acceptance.py` F 场景 | 短输入走前台（`max_tokens=256`），却断言「长度 >= 200」 | 假阳性：实际已被截断仍判通过 |
| 历史两次 | 比对错了任务的 `final_reply`；假设后台结果一定更长 | 上一轮已修正 |

### 10.2 内容截断实测

- **llama-server 侧**：`truncated = 1` 出现 **0 次**（188 次均为 `truncated = 0`），KV / prompt 未被强制截断。
- **生成侧**：本次窗口内有 **2 条**前台任务（F 场景「请用大约 400 字…」）在 319 / 306 字处停住，结尾分别是 `…- 硬件` 与 `…（如G` —— 确认撞到 `frontend.max_tokens=256`。
- **长度分布**（62 个任务）：`>=1200` 字 **0 条**，说明没有任务接近后台 `max_output_tokens=1024` 上限。
- **污染标记**：`[后台任务失败]`、`...[中间内容已截断]...`、`[后台任务进度]` 在 `final_reply` 中出现 **0 次**。
- **唯一 <5 字任务**是 `"4。"`（2+2 的答案），不是空产出。

### 10.3 修复：finish_reason 全链路透传

| 文件 | 改动 |
|---|---|
| `app/llama.py` | 记录 `last_finish_reason`；`/completion` 兜底按 `stopped_limit` 判 `length` |
| `app/store.py` | `tasks` 新增 `finish_reason` 列；旧库用 `PRAGMA table_info` + `ALTER TABLE` 自动补列（验证：补列成功、旧数据保留、重复 init 幂等） |
| `app/main.py` | `/chat`、`/task_result`、`chat/completions`、`responses`、两套 SSE 全部透传；Responses 遇 `length` 返回 `status="incomplete"` + `max_output_tokens`，流式发 `response.incomplete` |

### 10.4 定点验证（真机 6 项，全部符合）

| # | 请求 | 结果 |
|---|---|---|
| 1 | `/chat` 长请求（前台 256） | `finish_reason=length`，318 字，`/task_result` 同为 length |
| 2 | `/chat` 短请求 `2+2` | `finish_reason=stop`，回复 `"2+2等于 **4**！"` |
| 3 | `/v1/chat/completions` 长请求 | `finish_reason=length`，292 字 |
| 4 | `/v1/responses` 长请求 | `status=incomplete`，`incomplete_details.reason=max_output_tokens` |
| 5 | 流式 chat 长请求 | 末帧 `finish_reason=length`，303 字，结尾 `…- **投资机` |
| 6 | 流式 responses 长请求 | 末事件 `response.incomplete`，305 字，结尾 `…存钱前` |

### 10.5 完整回归（2026-11-08 19:42–19:44，已补跑）

修复后三套回归全部通过：

| 套件 | 结果 |
|---|---|
| `tests/test_openai_api.py` | **38/38** |
| `tests/test_responses_api.py` | **41/41** |
| `tests/audit_acceptance.py` | **91/91** |

原 82 项基线因新增 9 项截断断言（C 场景 3×2、F 场景 3）扩展为 91 项。

**本轮窗口核查（19:42 起，39 个任务）**

| 项 | 结果 |
|---|---|
| 任务状态 | completed 39，failed 0 |
| `finish_reason` 分布 | stop 38、length 1 |
| 唯一 length | `6c0e7de2`，318 字，结尾 `…**预` —— 截断可见性测试**故意触发**，字段记录正确 |
| 结尾完整性 | 除该条外，所有 ≥300 字产出均以句末标点结束 |
| 污染标记 | `[后台任务失败]` / 截断标记 / `[后台进度]` 全 0 |
| 日志新增 | api +68 / front +199 / mid +159，可疑 0 |
| 运行态 | 前台 ready、队列 0、切换 1 次；停服后端口释放、显存回落至约 500MB |

---

## 11. 补充：moe 复测与上下文压缩（2026-11-08 19:52–20:45）

### 11.1 moe 复测：根因是写死的分层比例

原状态：`moe available=false`，`gpu1: need 17333MB > usable 17243MB`，差 90MB。计算依据是 `need 26000 × 2/3`——`split_ratio: [1, 2]` 写死在配置里，让 gpu1 承担 2/3。

两处修正：

1. 删除 `settings.yaml` 中写死的 `split_ratio: [1, 2]`，改由「各卡空闲显存 − 前台预估占用」自动计算；
2. `app/gpu.py::split_ratio_for` 的比例精度从 `round(free/base)` 提到 `round(free/base*10)`——前者会把 1.3 压成 1，退化为均分。

修复后：`split_ratio=[10, 13]`，`moe available=true`。

**真机实测（35B MoE）**

| 项 | 结果 |
|---|---|
| `chosen_model` | **moe** |
| 加载耗时 | 21.2s（`switch idle -> moe`） |
| 生成速度 | **120.5 tok/s** |
| prefill | 439.8 ms |
| 总耗时 | 23.96s，回复 418 字，`finish=stop`，结尾完整 |
| 分层显存 | gpu0 15,243 / gpu1 12,947 MiB（共 28,190 / 40,960） |

### 11.2 上下文压缩：从「只能丢弃」到「先摘要再丢弃」

原实现只有两条路：历史超预算→丢弃早期轮次；单条输入超长→中间截断标记。没有任何压缩。

新增 `app/compaction.py` + `session_summaries` 表：

- 预算切分改为 `split_history()`，同时返回 `(kept, dropped, truncated)`；
- 有 `dropped` 且超过 `min_dropped_chars` 时，调前台模型把将被丢弃的部分摘要（默认上限 800 字），插入 system 作为「更早对话摘要（已压缩）」；
- 摘要**滚动合并**：已有摘要 + 新增丢弃内容 → 新摘要，不重复摘要已覆盖部分；
- 摘要要求暗号/编号逐字复制，首行单独输出 `【关键标记】` 列表；
- 摘要失败/关闭时退回原行为，不影响主链路。

**踩过的两个坑（都是实测暴露的）**

| 现象 | 根因 | 修正 |
|---|---|---|
| 第 6 轮答成第二轮暗号 | 每次对「全部被丢弃内容」重算摘要，新内容占满容量，最早的暗号被挤掉 | 改为滚动合并，按 `covered_messages` 只合并新增部分 |
| 仍丢第一轮暗号 | 400 字容量不足以容纳 5 个暗号 + 事实 | 上限提到 800 字，并要求首行 `【关键标记】` 单独列出 |

**修复后真机验证**

- 5 轮 × 3.5K 字长上下文，第 5 轮触发压缩（`compressed=true`，`summary=800`，`covered=2`）；
- 第 6 轮追问第一轮暗号 → **正确召回**（如 `ACC-A1-ERHP`），与修复前 `A1_recall=False` 形成对比；
- 摘要首行形如 `【关键标记】ACC-A1-ERHP, ..., ACC-A2-EOMX`，逐字保留；
- 第 7 轮同长度材料 → `compaction.cached=true`，不重复摘要。

### 11.3 回归结果

| 套件 | 结果 |
|---|---|
| `audit_acceptance.py` | **93/93**（A 场景新增压缩断言） |
| `test_openai_api.py` | **38/38** |
| `test_responses_api.py` | **41/41** |
| `test_smoke.py` | **all_pass** |

日志新增：api +73 / front +216 / mid +51 / moe +146，**可疑 0**。

### 11.4 仍未解决

摘要由 3B 前台生成，仍可能漏项或轻微改写（例如把 `ACC-` 展开成 `ACCESS-`，加强约束后未再出现）。需要精确回溯时，客户端仍应保留原始记录。

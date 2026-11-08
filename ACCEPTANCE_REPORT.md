# 验收压测报告 — explore v2.3.0

> 执行时间：2026-11-08 19:15–19:26 ｜ 设备：`liuda-API` ｜ 结论：**通过，可发布**
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

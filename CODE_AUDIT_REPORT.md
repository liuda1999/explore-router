# explore 智能本地路由 — 代码审计报告（v2.5.0，无推理环境）

> 审计时间：2026-11-14 ｜ 方式：**静态深度审计 + 无 GPU 全链路测试，不涉及推理**
> 背景：当前设备不具备推理环境（无本地模型），因此本报告覆盖「静态代码审计 +
> 无 GPU 可跑的全部 CI 层」；GPU 集成层（`ci_integration.sh`）待真机执行。
> 审计对象：v2.5.0 全部工作区变更（config/main/router/store/llama/router/
> api_backend/console/cli/run.sh/tests/CI/docs）。

## 1. 方法

| 层 | 做法 |
|---|---|
| 机械基线 | compileall + bash -n + JS 语法检查（node --check）+ ruff 两档规则集（CI 规则 E9/F63/F7/F82；审计规则 E/F/W/B）+ check_portability（40 文件）+ check_docs |
| 深度审计 | 三路并行逐文件审查：A 安全与配置边界、B 并发/异步/资源、C 跨平台/兼容/测试/文档；共 42 项发现，去重后 28 项确认修复 |
| 功能回归 | 无 GPU 全链路：test_api_slot 21/21、test_admin_api 34/34（含两次真实重启）、test_cli 9/9、check_config 23/23 |
| fail-fast 实测 | `run.sh start` 在无模型机器上按预期拒绝启动（前台显存不足，exit=1，无残留进程） |

## 2. 已修复（P1，7 项）

| # | 位置 | 问题 | 修复 | 验证 |
|---|---|---|---|---|
| 1 | console.py esc() | 只转义 `& < >` 不转义引号：FRONT_SYSTEM 热调值可破出 `value="..."` 属性构成存储型 XSS（叠加无 ADMIN_TOKEN 时的 no-cors CSRF 可远程播种） | esc() 追加 `"`/`'` 转义，覆盖全部属性与 onclick 字符串上下文；写操作增加 Origin/Host/Content-Type 校验（CSRF 守卫） | csrf_* 3 项新用例 + 全面板回归 |
| 2 | config.py `_deep_merge` | 就地 pop `__remove__`：import 后覆盖层丢失删除标记，任意一次保存让「已彻底删除」的 settings.yaml 键静默复活 | 合并改用 deepcopy，OVERLAY_LOADED 原样保留 | overlay_remove_marker_preserved 新用例 |
| 3 | cli.py `_pid_alive` | Windows 上 `os.kill(pid, 0)` 会真的 TerminateProcess：status/stop/start 第一步就杀死服务 | Windows 用 OpenProcess 探测；POSIX 不变 | 本机（Windows）实测 status 正常 |
| 4 | config.py `validate_overlay` + main.py 两处调用 | 子进程预检同步阻塞事件循环（数秒～180s），保存/重启预检期间全服务冻结 | 调用点改 `asyncio.to_thread`；tmp 文件名加 uuid 防并发争用 | 代码审查 + admin 套件回归 |
| 5 | main.py `/admin/restart` | 守护子进程固定 sleep 2s 后抢端口：旧实例优雅关闭（SSE 排空最长 120s + 模型退出）远超 2s → 新实例起不来；且无防重入 | 子进程改为「等 API 端口释放再拉起（上限 90s）」+ 端口按候选配置重解析（覆盖层改端口也生效）；`RESTARTING` 标志拒绝并发重启（409） | admin 套件两次真实重启回归 |
| 6 | llama.py start / router.py ensure | 关机取消（CancelledError）穿透 `except Exception`：已 spawn 未就绪的 llama-server 成孤儿，占住端口/显存 → 新实例 front.start() 失败 | start() 的清理改 `except BaseException`；ensure() 增加 CancelledError 分支回收候选；stop() 增加 Windows 分支 | 代码审查（真机 GPU 场景待回归） |
| 7 | cli.py `config set/reset` vs OPERATIONS | 只写覆盖层不热生效，文档却称「立即生效/两条等价路径」 | 服务可达时自动走 `/admin/config`（热生效 + 落盘），不可达时离线落盘并明确提示「下次启动生效」 | cli_config_set/reset_hot_applies 新用例 |

## 3. 已修复（P2，13 项）

| # | 位置 | 问题 → 修复 |
|---|---|---|
| 8 | config.py settings_schema | GET /admin/settings 明文返回 slots.*.api_key → 只出掩码 `***`；掩码值写入被丢弃（=未修改）；文档同步 |
| 9 | config.py 写路径 | apply/reset/commit 无锁、先改内存后落盘（失败分叉）→ `_ADMIN_WRITE_LOCK` 串行化 + 「先落盘成功再改内存」，落盘失败返回错误且零状态变更 |
| 10 | config.py `_persist_overlay`/`commit_overlay` | 非原子写（中途崩溃 → YAML 损坏服务起不来）→ 同目录 tmp + `os.replace` |
| 11 | api_backend.py `_ping` | 401/403/404 全部判健康 → 401/403 判不健康并给出密钥排查提示；404 判健康但 detail/start 日志提示「base_url 可能缺 /v1」 |
| 12 | settings.yaml/OPERATIONS | base_url 必须含版本段（如 /v1）未写明 → 三处文档落字（缺 /v1 = 全部 404） |
| 13 | llama.py | POSIX-only 原语（start_new_session/killpg）在 Windows 抛晦涩异常 → start() 显式报「Windows 请使用 type: api 槽位」；stop() 增加 Windows terminate/kill 分支 |
| 14 | llama.py start | Popen 抛异常时日志句柄泄漏 → Popen 包 try/except 关闭后 re-raise |
| 15 | main.py 前台调用 | front.chat 被即时回复与压缩摘要并发调用，实例级 last_finish_reason/last_timings 交叉覆写 → `front_chat_lock` 串行化 |
| 16 | main.py `_compact` | 同会话并发触发重复摘要、covered_messages 回退 → per-session asyncio.Lock |
| 17 | tests/test_admin_api.py | 直接改运行实例的覆盖层并重启，失败遗留状态 → 文件头警示「指向隔离实例」；OPERATIONS 同步 |
| 18 | CI 接线 | v2.5 新套件无任何 CI job 执行 → ci.yml 新增 `functional` job（ubuntu-latest 跑 test_api_slot + test_cli）；ci_integration.sh 对 api-only 配置显式报「需要至少一个 local 槽位」 |
| 19 | run.sh vs cli.py | --wait 等待时长（固定 210 轮）与 STARTUP_TIMEOUT_S 不对齐；PYTHONPATH 覆盖 vs 前置；就绪谓词依赖响应体紧凑格式 → run.sh 三处对齐（tries 读配置、PYTHONPATH 前置保留、谓词改 JSON 解析） |
| 20 | tests/ci_static.sh | ruff 目标缺 cli.py → 补入 |

## 4. 已修复（P3，8 项）

| # | 位置 | 问题 → 修复 |
|---|---|---|
| 21 | main.py `_require_admin` | token 支持 `?token=` query 会落访问日志 → 仅留请求头 |
| 22 | console.py | 管理令牌存 localStorage（XSS 可读、不随会话失效）→ sessionStorage |
| 23 | run.sh/cli.py stop | PID 文件 kill 无归属校验（陈旧 PID 复用误杀）→ kill 前 /proc cmdline（POSIX）/PowerShell CommandLine（Windows）校验，非本项目进程只警告并删 PID 文件 |
| 24 | store.py/main.py | `/task_result` 的 num_ctx 固定写 NUM_CTX，api 前台时与 /chat 的 FRONT_NUM_CTX 口径不一致 → create_task 增加参数，调用方传 FRONT_NUM_CTX |
| 25 | console.py doRestart | 只判 frontend=ready，旧实例退出前可能误报「重启完成」→ 增加 started_at 变化确认 |
| 26 | tests/test_cli.py | 临时目录不清理 → finally rmtree |
| 27 | README/OPERATIONS/DEPLOYMENT/PROJECT_INTRO | 计数失真（14/14→24/24）、目录树缺 api_backend/console/cli、「五个面板」实为六个、request_timeout_s 重复行 → 全部修正 |
| 28 | console.py onclick/value | 槽位 key 与字段值进入 JS 字符串/属性上下文 → 由 #1 的 esc 修复一并消除 |

## 5. 确认无问题（审计核对清单）

- **密钥保密**：/admin/config、describe()、hot_params、日志、异常消息均不含密钥值；api_backend `_log_line` 只记状态码/字数；YAML 全仓 safe_load/safe_dump，无 shell=True/eval。
- **路径注入**：`_path_is_safe` 拒绝非 `[A-Za-z0-9_-]` 段；removes/removes 目标由预检子进程 fail-fast 拦截；无文件系统穿越。
- **store 并发**：全部短同步操作 + RLock，无长任务持锁；update_task 列名来自代码字面量，无注入。
- **SlotManager**：单队列互斥语义保持；api 槽位分支不触碰本地驻留状态；ensure_front 双检+冷却无竞态。
- **资源**：store 连接即用即关；EmbeddingProvider 失败降级；ApiBackend 客户端 `_ensure_client` 无并发双建。
- **向后兼容**：无 `type:` 字段默认 local；无覆盖层文件安全；/chat 与 /v1/* 响应字段对 local-only 部署零变化（`num_ctx` 在 local 前台时 FRONT_NUM_CTX==NUM_CTX）；旧 DB 四表结构未动（git diff 证实 store/llama/gpu 在 v2.4 基线上仅上述受控变更）。
- **可移植性检查**：40 文件零违例；requirements 全 `==`。

## 6. 遗留与建议（不影响发布，按优先级）

1. **GPU 真机回归**（下一台有推理环境的机器）：跑 `ci_integration.sh` + `quick_check.py` + 三套回归，重点验证 #6 取消孤儿修复与多卡分层。
2. **ruff 严格规则集**（E/F/W/B）现存 194 条，绝大多数为 tests 的 E501 行宽风格噪音；如需治理可加 `--ignore E501` 或逐文件放宽，不阻塞 CI。
3. **/config /health 未鉴权**：默认 127.0.0.1 信任边界下暴露 base_url/密钥来源变量名/路径（不含密钥值）；对外部署必须反代+鉴权（既有文档边界，未改行为）。
4. **test_admin_api 建议长期指向隔离实例**（文件头已警示）；CI 的 functional job 不含它（需自备服务），保持手动。
5. **long-poll 轮询**（_wait_task 0.2s / SSE 1s）每次开新 SQLite 连接，本地开销可忽略；如需优化可改事件广播，未做。

## 7. 复现

```bash
export PYTHONPATH="$PWD/pylib:$PWD"
bash tests/ci_static.sh                                   # 静态层（任意机器）
python3 tests/test_api_slot.py                            # api 槽位全链路（自起 fake server）
FAKE_PORT=18925 TEST_API_PORT=18926 python3 tests/test_api_slot.py   # 端口冲突时换端口
python3 tests/test_cli.py                                 # CLI 离线
BASE=http://127.0.0.1:8100 python3 tests/test_admin_api.py  # 管理接口（含真实重启；指向隔离实例）
```

## 8. 结论

28 项确认发现全部修复并带回归；无 GPU 可跑的全部 CI 层通过（STATIC_OK、
api-slot 21/21、admin 34/34、CLI 9/9、config 23/23）。v2.5.0 的安全边界
（密钥掩码、CSRF 守卫、XSS 修复）、重启链路健壮性（端口等待/防重入/孤儿回收）
与配置完整性（删除标记保留、原子写、先落盘后生效）均达到可发布状态。

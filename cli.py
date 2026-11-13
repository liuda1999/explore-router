#!/usr/bin/env python3
"""explore CLI：服务管理、配置热调、请求调试。

与 run.sh 同一套语义（PID 文件、端口预检、前台就绪轮询），用 Python 实现，
便于在无 bash 的环境使用；配置命令直接读写 settings.local.yaml 覆盖层，
服务在运行时经 /admin/config 热生效。

用法示例:
  python3 cli.py start --wait          # 启动并等前台就绪
  python3 cli.py status                # 查看运行状态
  python3 cli.py config set FRONT_MAX_TOKENS 320
  python3 cli.py config get limits     # 查看生效配置片段
  python3 cli.py config validate       # 离线校验配置
  python3 cli.py chat "2+2 等于几" --session demo
  python3 cli.py check-api cloud       # 测试 api 槽位连通性
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PID_FILE = Path(os.getenv("PID_FILE", str(ROOT / "data" / "router.pid")))
LOG_FILE = Path(os.getenv("LOG", str(ROOT / "logs" / "api.log")))


def _config():
    sys.path.insert(0, str(ROOT))
    from app import config
    return config


def _ports():
    try:
        c = _config()
        return (int(os.getenv("API_PORT", c.API_PORT)),
                int(os.getenv("FRONT_PORT", c.FRONT_PORT)),
                int(os.getenv("SLOT_PORT", c.SLOT_PORT)))
    except Exception:
        return (int(os.getenv("API_PORT", "8100")),
                int(os.getenv("FRONT_PORT", "18100")),
                int(os.getenv("SLOT_PORT", "18101")))


def _http(method, path, body=None, timeout=30, expect_json=True):
    """对本地 API 的极简 HTTP 客户端（仅标准库，无需 pylib 的 httpx）。"""
    api_port = _ports()[0]
    url = f"http://127.0.0.1:{api_port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    token = os.getenv("ADMIN_TOKEN", "")
    if token:
        headers["X-Admin-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except Exception:
            detail = raw
        raise SystemExit(f"HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"服务不可达（{url}）：{e.reason}\n"
                         f"提示：先 python3 cli.py start --wait")
    return json.loads(raw) if expect_json and raw else raw


def _pid_alive(pid):
    """探测进程是否存活。

    注意：Windows 上 os.kill(pid, 0) 会真的 TerminateProcess（Python 文档
    明确行为），绝不能用；POSIX 用 signal 0 探测，Windows 用 OpenProcess。
    """
    if pid is None:
        return False
    pid = int(pid)
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError, PermissionError):
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    k32.CloseHandle(handle)
    return True


def _pid_is_ours(pid):
    """PID 归属校验：命令行包含本工作区（或 python 特征）才允许 kill，
    防止服务崩溃后 PID 被其它进程复用时误杀。"""
    pid = int(pid)
    if os.name == "posix":
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(
                b"\0", b" ").decode("utf-8", "replace")
        except Exception:
            return False, ""
        ours = str(ROOT) in cmd or "uvicorn" in cmd
        return ours, cmd.strip()[:120]
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}')"
             f".CommandLine"],
            capture_output=True, text=True, timeout=15)
        cmd = (r.stdout or "").strip()
    except Exception:
        cmd = ""
    ours = bool(cmd) and (str(ROOT) in cmd or "uvicorn" in cmd)
    return ours, cmd[:120]


def _port_busy(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ---- 服务管理 ---------------------------------------------------------------

def cmd_start(args):
    if _pid_alive(PID_FILE.read_text().strip() if PID_FILE.exists() else None):
        print(f"already running pid={PID_FILE.read_text().strip()}")
        return
    api_port, _fp, _sp = _ports()
    if _port_busy(api_port):
        raise SystemExit(f"ERROR: port {api_port} already in use；"
                         f"请改 settings.yaml 的 runtime.ports.api 或先释放端口")
    cfg = _config()
    plan = cfg.VRAM_PLAN.get(cfg.FRONT_KEY, {})
    if cfg.FRONT_TYPE == "local" and not plan.get("available", True):
        raise SystemExit(f"ERROR: 前台槽位 {cfg.FRONT_KEY} 显存不足: "
                         f"{plan.get('reason')}\n"
                         f"请释放显存，或调整 settings.yaml 的模型/量化/vram_mb")
    env = os.environ.copy()
    pylib = str(ROOT / "pylib")
    env["PYTHONPATH"] = pylib + os.pathsep + str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "ab")
    kwargs = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(api_port), "--workers", "1"],
        stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT), env=env, **kwargs)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(proc.pid))
    print(f"started pid={proc.pid} api={api_port} log={LOG_FILE}")
    if args.wait:
        deadline = time.time() + cfg.STARTUP_TIMEOUT_S
        while time.time() < deadline:
            try:
                h = _http("GET", "/health", timeout=3)
                if h.get("frontend") == "ready":
                    print("ready")
                    return
            except SystemExit:
                pass
            time.sleep(2)
        raise SystemExit(f"NOT READY in {cfg.STARTUP_TIMEOUT_S}s; see {LOG_FILE}")


def cmd_stop(_args):
    pid = None
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
    if not _pid_alive(pid):
        print(f"not running (pid file: {PID_FILE})")
        PID_FILE.unlink(missing_ok=True)
        return
    pid = int(pid)
    # 归属校验：服务崩溃后 PID 可能被其它进程复用，kill 前确认命令行属于本项目
    ours, cmd = _pid_is_ours(pid)
    if not ours:
        print(f"WARNING: pid={pid} 不是本项目进程（{cmd or '读取失败'}），跳过 kill；"
              f"请手动确认后删除 {PID_FILE}")
        PID_FILE.unlink(missing_ok=True)
        return
    print(f"stopping pid={pid}")
    if os.name == "posix":
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(1)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
    else:
        # Windows：taskkill /T 连同子进程一起结束
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)
    PID_FILE.unlink(missing_ok=True)
    # POSIX 兜底：清理「命令行包含本工作区 + 本项目运行时二进制」的残留模型进程
    # （同 run.sh 的安全规则：绝不全局清理）
    if os.name == "posix":
        try:
            cfg = _config()
            marker = str(cfg.LLAMA_SERVER)
        except Exception:
            marker = "llama-server"
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmd = (entry / "cmdline").read_bytes().replace(
                    b"\0", b" ").decode("utf-8", "replace")
            except Exception:
                continue
            if str(ROOT) in cmd and marker in cmd:
                print(f"kill leftover model process pid={entry.name}")
                os.kill(int(entry.name), signal.SIGTERM)
        print("stopped")
        return
    print("stopped")


def cmd_restart(args):
    cmd_stop(args)
    args.wait = getattr(args, "wait", False)
    cmd_start(args)


def cmd_status(_args):
    pid = PID_FILE.read_text().strip() if PID_FILE.exists() else None
    api_port, front_port, slot_port = _ports()
    if _pid_alive(pid):
        print(f"running pid={pid} api={api_port} front={front_port} slot={slot_port}")
    else:
        print(f"not running (api={api_port} front={front_port} slot={slot_port})")


def cmd_health(_args):
    h = _http("GET", "/health", timeout=10)
    print(json.dumps(
        {k: h[k] for k in ("frontend", "backend", "num_ctx", "queue_depth",
                           "slot", "vram") if k in h},
        ensure_ascii=False, indent=2))


# ---- 配置 -------------------------------------------------------------------

def _dotted_get(data, path):
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                raise SystemExit(f"配置无此键: {path}（可用: python3 cli.py config get）")
            cur = cur[part]
        else:
            raise SystemExit(f"配置无此键: {path}")
    return cur


def _service_reachable():
    try:
        _http("GET", "/health", timeout=3)
        return True
    except SystemExit:
        return False


def cmd_config(args):
    cfg = _config()
    if args.action == "get":
        data = _try_admin_get() or cfg.describe()
        out = _dotted_get(data, args.key) if args.key else data
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.action == "set":
        try:
            value = json.loads(args.value)
        except (ValueError, TypeError):
            value = args.value
        # 服务在跑：走 /admin/config 热生效（否则只写覆盖层，服务感知不到）
        if _service_reachable():
            r = _http("POST", "/admin/config", body={args.key: value})
            print(f"已热生效并持久化: {args.key} = "
                  f"{json.dumps(r['applied'][args.key], ensure_ascii=False)}")
            return
        applied, err = cfg.apply_runtime_params({args.key: value})
        if err:
            raise SystemExit(f"设置失败: {err}")
        print(f"服务未运行：已写入 {cfg.OVERLAY_FILE}（下次启动生效）: "
              f"{args.key} = {json.dumps(applied[args.key], ensure_ascii=False)}")
    elif args.action == "reset":
        if _service_reachable():
            r = _http("DELETE", f"/admin/config/{args.key}")
            print(f"已恢复基础值（热生效）: {args.key} = "
                  f"{json.dumps(r['value'], ensure_ascii=False)}")
            return
        value, err = cfg.reset_runtime_param(args.key)
        if err:
            raise SystemExit(f"重置失败: {err}")
        print(f"服务未运行：已恢复基础值（下次启动生效）: {args.key} = "
              f"{json.dumps(value, ensure_ascii=False)}")
    elif args.action == "overrides":
        print(f"覆盖层文件: {cfg.OVERLAY_FILE}")
        if cfg.OVERLAY_FILE.exists():
            print(cfg.OVERLAY_FILE.read_text(encoding="utf-8"))
        else:
            print("（尚无覆盖层）")
    elif args.action == "validate":
        d = cfg.describe()
        print(json.dumps(
            {k: d[k] for k in ("backend", "gpu_count", "num_ctx", "num_ctx_source",
                               "front_slot", "front_type", "front_num_ctx",
                               "tier_order", "tier_to_slot", "tier_thresholds",
                               "tier_fallbacks", "vram_plan")},
            ensure_ascii=False, indent=2))
        print("CONFIG_OK")


def _try_admin_get():
    try:
        return _http("GET", "/admin/config", timeout=5)
    except SystemExit:
        return None


# ---- 请求调试 ---------------------------------------------------------------

def cmd_chat(args):
    if args.wait > 0:
        r = _http("POST", "/v1/chat/completions", timeout=max(30, args.wait + 10),
                  body={"model": "explore",
                        "messages": [{"role": "user", "content": args.text}]})
        meta = r.get("explore") or {}
        content = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        print("=== instant_reply（前台） ===")
        print(meta.get("instant_reply") or content)
        print("=== final（后台结果，原样递回） ===")
        print(content if meta.get("process_mode") == "async" else
              "（简单任务由前台直答）")
        print("=== meta ===")
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return
    body = {"user_input": args.text, "session_id": args.session}
    if args.tier:
        body["prefer_tier"] = args.tier
    r = _http("POST", "/chat", timeout=60, body=body)
    print("=== instant_reply（前台） ===")
    print(r.get("instant_reply") or "")
    print("=== final（后台结果） ===")
    print(r.get("最终回复") or "（后台任务进行中，稍后用 task 查询）")
    print("=== meta ===")
    print(json.dumps({k: r.get(k) for k in
                      ("task_id", "task_type", "process_mode", "selection_reason")},
                     ensure_ascii=False, indent=2))


def cmd_task(args):
    r = _http("GET", f"/task_result?task_id={args.task_id}", timeout=10)
    print(json.dumps({k: r.get(k) for k in
                      ("task_id", "task_status", "progress", "tier", "chosen_model",
                       "instant_reply", "final_reply", "finish_reason",
                       "release_info", "error_msg")},
                     ensure_ascii=False, indent=2))


def cmd_tasks(args):
    r = _http("GET", f"/tasks?limit={args.limit}", timeout=10)
    for t in r.get("tasks") or []:
        print(f"{t.get('task_id', '')[:8]}  {t.get('status', ''):<10} "
              f"{t.get('tier') or '':<8} {t.get('model_name') or '':<8} "
              f"{t.get('progress', 0):>3}%  {(t.get('created_at') or '')}")


def cmd_models(_args):
    r = _http("GET", "/v1/models", timeout=10)
    for m in r.get("data") or []:
        print(m.get("id"))


def cmd_logs(args):
    name = args.name or "api"
    log_dir = ROOT / "logs"
    try:
        log_dir = _config().LOGS
    except Exception:
        pass
    path = log_dir / f"{name}.log"
    if not path.exists():
        raise SystemExit(f"日志不存在: {path}（可用名称: api, front, <slot key>）")
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 64 * 1024))
        lines = f.read().decode("utf-8", "replace").splitlines()
    print("\n".join(lines[-args.tail:]))


def cmd_check_api(args):
    cfg = _config()
    meta = cfg.MODELS_META.get(args.slot_key)
    if not meta:
        raise SystemExit(f"槽位不存在: {args.slot_key}（现有: "
                         f"{', '.join(cfg.MODELS_META)}）")
    if meta.get("type") != "api":
        raise SystemExit(f"槽位 {args.slot_key} 是 local 类型，无需 check-api")
    from app.api_backend import ApiBackend
    backend = ApiBackend(args.slot_key, meta["base_url"], meta["model_ref"],
                         api_key=cfg.api_key_for(meta),
                         context_window=meta.get("context_window"))

    async def _run():
        import asyncio
        ok, detail = await backend._ping()
        print(f"ping {meta['base_url']}/models -> {'OK' if ok else 'FAIL'} ({detail})")
        if not ok:
            return
        print("试答中…")
        reply = await backend.chat(
            [{"role": "user", "content": "用一个词回答：1+1等于几？"}],
            max_tokens=32, temperature=0.1)
        print(f"reply ({backend.last_finish_reason}): {reply}")
        await backend.stop()

    import asyncio
    asyncio.run(_run())


# ---- 入口 -------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="explore 智能本地路由 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start", help="启动服务（写 PID 文件）")
    sp.add_argument("--wait", action="store_true", help="等待前台就绪")
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("stop", help="停止服务")
    sp.set_defaults(fn=cmd_stop)
    sp = sub.add_parser("restart", help="重启服务")
    sp.add_argument("--wait", action="store_true", help="等待前台就绪")
    sp.set_defaults(fn=cmd_restart)
    sp = sub.add_parser("status", help="运行状态")
    sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("health", help="健康摘要")
    sp.set_defaults(fn=cmd_health)

    sp = sub.add_parser("config", help="查看/热调配置")
    sp.add_argument("action", choices=["get", "set", "reset", "overrides", "validate"])
    sp.add_argument("key", nargs="?", help="参数名或 describe() 的点路径")
    sp.add_argument("value", nargs="?", help="set 的新值（JSON 或字符串）")
    sp.set_defaults(fn=cmd_config)

    sp = sub.add_parser("chat", help="发送一条调试消息")
    sp.add_argument("text")
    sp.add_argument("--session", default="cli")
    sp.add_argument("--tier", default=None, help="prefer_tier（档位名）")
    sp.add_argument("--wait", type=int, default=0,
                    help=">0 时走 OpenAI final 模式等待后台结果 N 秒")
    sp.set_defaults(fn=cmd_chat)

    sp = sub.add_parser("task", help="查询任务结果")
    sp.add_argument("task_id")
    sp.set_defaults(fn=cmd_task)
    sp = sub.add_parser("tasks", help="最近任务列表")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_tasks)
    sp = sub.add_parser("models", help="虚拟模型名列表")
    sp.set_defaults(fn=cmd_models)

    sp = sub.add_parser("logs", help="查看日志尾部（api/front/<slot>）")
    sp.add_argument("name", nargs="?", default="api")
    sp.add_argument("--tail", type=int, default=40)
    sp.set_defaults(fn=cmd_logs)

    sp = sub.add_parser("check-api", help="api 槽位连通性测试 + 单轮试答")
    sp.add_argument("slot_key")
    sp.set_defaults(fn=cmd_check_api)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

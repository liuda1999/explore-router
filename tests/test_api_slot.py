"""api 槽位与 api 前台全链路测试（fake 服务端 = 真实 HTTP，无 GPU 依赖）。

自备环境并真实走通整条链路：
1. 临时目录生成 fixture settings.yaml：front / mid / cloud 三个槽位全部
   type: api 指向本地 fake OpenAI 服务端；tier_order 含第 4 档 deep；
2. 启动 fake server + uvicorn（独立端口与数据目录）；
3. 验证 /chat、/v1/chat/completions（final/instant/SSE）、/v1/responses、
   第 4 档分级、结果原样递回、前台降级文案、控制台与 /admin 可用；
4. 结束自动停掉全部子进程。

用法: python3 tests/test_api_slot.py
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAKE_PORT = int(os.getenv("FAKE_PORT", "18900"))
API_PORT = int(os.getenv("TEST_API_PORT", "18901"))
BASE = f"http://127.0.0.1:{API_PORT}"
FAKE_BASE = f"http://127.0.0.1:{FAKE_PORT}/v1"

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append({"case": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:200]}")


def http(method, url, body=None, timeout=60, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read().decode("utf-8", "replace")
            return payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        if raw:
            return payload
        try:
            return {"_status": e.code, **json.loads(payload)}
        except Exception:
            return {"_status": e.code, "_raw": payload}


def wait_port(port, timeout=30):
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def poll_task(task_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = http("GET", f"{BASE}/task_result?task_id={task_id}")
        if t.get("task_status") in ("completed", "failed"):
            return t
        time.sleep(0.5)
    return t


def write_fixture(tmp):
    cfg = {
        "runtime": {
            "server_bin": "llama-server", "lib_dir": "",
            "gpu_backend": "auto", "n_gpu_layers": 99,
            "max_output_tokens": 1024, "queue_maxsize": 32,
            "ports": {"api": API_PORT, "front": API_PORT + 10,
                      "slot": API_PORT + 11},
            "startup_timeout_s": 120, "request_timeout_s": 120,
        },
        "context": {"num_ctx": 32768, "kv_cache": "q8_0",
                    "compaction": {"enabled": False,
                                   "max_summary_chars": 800,
                                   "min_dropped_chars": 600}},
        "embedding": {"onnx": "models/embedding/model.onnx",
                      "tokenizer": "models/embedding/tokenizer.json"},
        "budget": {"reserve_system_mb": 1024, "headroom_ratio": 0.10},
        "slots": {
            "front": {"type": "api", "base_url": FAKE_BASE,
                      "model": "fake-front", "context_window": 32768,
                      "role": "front", "resident": True},
            "mid": {"type": "api", "base_url": FAKE_BASE,
                    "model": "fake-cloud", "context_window": 32768,
                    "role": "middle"},
            "cloud": {"type": "api", "base_url": FAKE_BASE,
                      "model": "fake-cloud", "context_window": 65536,
                      "max_output_tokens": 2048, "role": "heavy"},
        },
        "routing": {
            "tier_order": ["simple", "mid", "heavy", "deep"],
            "tier_to_slot": {"simple": "front", "mid": "mid",
                             "heavy": "cloud", "deep": "cloud"},
            "tier_thresholds": {"mid": 25, "heavy": 120, "deep": 4000},
            "default_slot": "mid",
            "mid_min_chars": 25, "heavy_min_chars": 120,
            "max_input_chars": "auto",
            "complex_words": ["详细", "分析", "方案"],
        },
        "openai": {"default_mode": "final", "wait_timeout_s": 60},
        "frontend": {
            "max_tokens": 256, "history_budget_ratio": 0.45,
            "history_min_chars": 2048, "history_max_messages": 40,
            "context_safety_tokens": 256, "restart_cooldown_s": 30,
            "progress_window_s": 1800,
            "system_prompt": "你是测试前台，简洁回应。",
        },
    }
    path = pathlib.Path(tmp) / "settings.yaml"
    import yaml
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    (pathlib.Path(tmp) / "settings.local.yaml").write_text("", encoding="utf-8")
    return path


def main():
    import shutil
    tmp = tempfile.mkdtemp(prefix="explore-api-slot-")
    fake_proc = api_proc = None
    try:
        # 端口被其它进程占用会让宕机场景失真（terminate 的是绑定失败的子进程）
        if wait_port(FAKE_PORT, timeout=1):
            raise SystemExit(
                f"端口 {FAKE_PORT} 已被占用（可能是另一个 fake server 或服务实例）。"
                f"请先释放端口，或用 FAKE_PORT=<空闲端口> 重跑本测试。")
        settings_path = write_fixture(tmp)
        # 1) fake server
        fake_proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "fake_openai_server.py"),
             "--port", str(FAKE_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port(FAKE_PORT):
            raise SystemExit("fake server 未就绪")
        # 2) 被测服务
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "pylib")]),
            "SETTINGS_FILE": str(settings_path),
            "SETTINGS_LOCAL_FILE": str(pathlib.Path(tmp) / "settings.local.yaml"),
            "DATA_DIR": str(pathlib.Path(tmp) / "data"),
            "LOG_DIR": str(pathlib.Path(tmp) / "logs"),
            "API_PORT": str(API_PORT),
            "FRONT_PORT": str(API_PORT + 10),
            "SLOT_PORT": str(API_PORT + 11),
        })
        env.pop("NUM_CTX", None)
        log = open(pathlib.Path(tmp) / "uvicorn.out", "ab")
        api_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(API_PORT), "--workers", "1"],
            env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT))
        ready = False
        for _ in range(60):
            try:
                if http("GET", f"{BASE}/health", timeout=3).get("frontend") == "ready":
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(1)
        if not ready:
            print((pathlib.Path(tmp) / "uvicorn.out").read_text(
                encoding="utf-8", errors="replace")[-2000:])
            raise SystemExit("服务未就绪")
        check("service_ready", True, f"base={BASE}")

        # ---- 健康与配置 ----
        h = http("GET", f"{BASE}/health")
        c = h.get("config") or {}
        check("front_is_api", c.get("front_type") == "api"
              and c.get("front_num_ctx") == 32768, c.get("front_type"))
        check("slot_types", {k: v.get("type") for k, v in
                             (c.get("slots") or {}).items()}
              == {"front": "api", "mid": "api", "cloud": "api"},
              {k: v.get("type") for k, v in (c.get("slots") or {}).items()})
        check("vram_plan_api_available",
              all(v.get("available") for v in (c.get("vram_plan") or {}).values()),
              c.get("vram_plan"))
        check("console_page",
              "explore" in http("GET", f"{BASE}/console", raw=True),
              "GET /console")
        admin = http("GET", f"{BASE}/admin/config")
        check("admin_hot_params", bool(admin.get("hot_params")),
              f"{len(admin.get('hot_params') or {})} params")

        # ---- 模型名 ----
        models = [m.get("id") for m in
                  (http("GET", f"{BASE}/v1/models").get("data") or [])]
        check("models_list",
              all(m in models for m in
                  ("explore", "explore-fast", "explore-simple", "explore-mid",
                   "explore-heavy", "explore-deep")),
              models)

        # ---- /chat：简单任务前台直答 ----
        r = http("POST", f"{BASE}/chat", body={
            "user_input": "1+1 等于几", "session_id": "api-slot-t1"})
        check("chat_simple_direct",
              r.get("process_mode") == "direct"
              and "FRONT-OK" in (r.get("instant_reply") or "")
              and r.get("task_status") == "completed"
              and r.get("finish_reason") == "stop",
              {k: r.get(k) for k in ("process_mode", "task_status",
                                     "instant_reply")})

        # ---- /chat：复杂任务走后台 api 槽位，结果原样递回 ----
        long_text = "请详细分析 REM-M1 的处理方案。" + "这是一段足够长的文本。" * 30
        r = http("POST", f"{BASE}/chat", body={
            "user_input": long_text, "session_id": "api-slot-t1",
            "prefer_tier": "heavy"})
        task_id = r.get("task_id")
        t = poll_task(task_id)
        check("chat_async_cloud",
              r.get("process_mode") == "async" and t.get("task_status") == "completed"
              and t.get("chosen_model") == "cloud"
              and "CLOUD-OK" in (t.get("final_reply") or "")
              and "REM-M1" in (t.get("final_reply") or ""),
              {k: t.get(k) for k in ("task_status", "chosen_model", "final_reply")})
        check("final_verbatim_differs_from_instant",
              t.get("final_reply") and t.get("final_reply") != t.get("instant_reply")
              and "FRONT-OK" not in (t.get("final_reply") or ""),
              {"instant_head": (t.get("instant_reply") or "")[:40],
               "final_head": (t.get("final_reply") or "")[:40]})

        # ---- OpenAI final：content 与 final_reply 逐字一致 ----
        marker = "REM-V1"
        r = http("POST", f"{BASE}/v1/chat/completions", body={
            "model": "explore",
            "messages": [{"role": "user",
                          "content": f"请分析 {marker} 并输出完整方案" + "。" * 150}]})
        meta = r.get("explore") or {}
        content = ((r.get("choices") or [{}])[0].get("message") or {}).get("content")
        t = poll_task(meta.get("task_id")) if meta.get("task_id") else {}
        check("openai_final_verbatim",
              meta.get("process_mode") == "async"
              and content == t.get("final_reply")
              and marker in (content or ""),
              {"content_head": (content or "")[:60],
               "final_head": (t.get("final_reply") or "")[:60]})

        # ---- OpenAI instant：立即返回即时回复 + task_id ----
        t0 = time.time()
        r = http("POST", f"{BASE}/v1/chat/completions", body={
            "model": "explore-fast",
            "messages": [{"role": "user", "content": "请详细分析方案" + "。" * 150}]})
        meta = r.get("explore") or {}
        instant_content = ((r.get("choices") or [{}])[0].get("message") or {}).get("content")
        check("openai_instant",
              meta.get("process_mode") == "async" and meta.get("task_id")
              and "FRONT-OK" in (instant_content or "")
              and time.time() - t0 < 20,
              {"elapsed_s": round(time.time() - t0, 1),
               "task_id": meta.get("task_id")})
        if meta.get("task_id"):
            t = poll_task(meta["task_id"])
            check("openai_instant_final_ready",
                  t.get("task_status") == "completed"
                  and "CLOUD-OK" in (t.get("final_reply") or ""),
                  t.get("final_reply", "")[:60])

        # ---- OpenAI 流式：SSE 分块 + [DONE] ----
        raw = http("POST", f"{BASE}/v1/chat/completions", body={
            "model": "explore", "stream": True,
            "messages": [{"role": "user", "content": "你好"}]}, raw=True)
        check("openai_stream_sse",
              raw.startswith("data: ") and "data: [DONE]" in raw
              and '"finish_reason":"stop"' in raw.replace(" ", ""),
              raw[:80])

        # ---- Responses API + previous_response_id 续接召回 ----
        r1 = http("POST", f"{BASE}/v1/responses", body={
            "model": "explore", "input": "请记住暗号 REM-R1 并确认。",
            "store": True})
        resp_id = r1.get("id")
        r2 = http("POST", f"{BASE}/v1/responses", body={
            "model": "explore", "input": "刚才的暗号是什么？",
            "previous_response_id": resp_id})
        out2 = ""
        for item in (r2.get("output") or []):
            for part in (item.get("content") or []):
                if part.get("type") == "output_text":
                    out2 += part.get("text") or ""
        check("responses_chain_recall",
              str(resp_id).startswith("resp_")
              and r2.get("status") == "completed"
              and "REM-R1" in out2,
              {"id": resp_id, "out2_head": out2[:80]})

        # ---- 第 4 档：显式 prefer + 阈值自动分级 ----
        r = http("POST", f"{BASE}/chat", body={
            "user_input": "deep 任务", "session_id": "t4", "prefer_tier": "deep"})
        check("prefer_deep_tier",
              r.get("task_type") == "deep"
              and (t := poll_task(r.get("task_id"))).get("chosen_model") == "cloud",
              {"tier": r.get("task_type"), "model": t.get("chosen_model")})
        r = http("POST", f"{BASE}/chat", body={
            "user_input": "x" * 4200, "session_id": "t4b"})
        check("auto_deep_by_threshold", r.get("task_type") == "deep",
              {"tier": r.get("task_type"), "reason": r.get("selection_reason")})

        # ---- 前台降级：fake server 宕机 -> front_failure 兜底；恢复后自愈 ----
        fake_proc.terminate()
        fake_proc.wait(timeout=10)
        time.sleep(0.5)
        r = http("POST", f"{BASE}/chat", body={
            "user_input": "还在吗", "session_id": "t5"})
        check("front_failure_fallback",
              r.get("code") == 200
              and "前台模型暂时不可用" in (r.get("instant_reply") or ""),
              (r.get("instant_reply") or "")[:80])
        t = poll_task(r.get("task_id"))
        check("front_failure_direct_completes",
              t.get("task_status") == "completed",
              {"status": t.get("task_status")})
        # 宕机窗口提交重档任务：走后台 api 槽位，必须干净地 failed 且带原因
        r2 = http("POST", f"{BASE}/chat", body={
            "user_input": "请详细分析以下方案并给出完整结论：" + "宕机测试。" * 30,
            "session_id": "t5", "prefer_tier": "heavy"})
        t2 = poll_task(r2.get("task_id"))
        check("api_task_fails_cleanly",
              t2.get("task_status") == "failed" and (t2.get("error_msg") or ""),
              {"status": t2.get("task_status"),
               "err": (t2.get("error_msg") or "")[:80]})
        fake_proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "fake_openai_server.py"),
             "--port", str(FAKE_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_port(FAKE_PORT)
        r = http("POST", f"{BASE}/chat", body={
            "user_input": "恢复了吗", "session_id": "t5"})
        check("recovers_after_server_back",
              "FRONT-OK" in (r.get("instant_reply") or ""),
              (r.get("instant_reply") or "")[:60])

    finally:
        for p in (api_proc, fake_proc):
            if p:
                try:
                    p.terminate()
                    p.wait(timeout=10)
                except Exception:
                    p.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for x in RESULTS if x["ok"])
    print(f"\n=== API_SLOT {passed}/{len(RESULTS)} ===")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

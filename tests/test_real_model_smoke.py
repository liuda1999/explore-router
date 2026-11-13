"""真实模型冒烟测试：用本机 Ollama 的模型作为全部 api 槽位，跑通所有功能。

与 fake server 套件不同：本脚本的全部请求都是**真实推理**（OpenAI 兼容端点
= 本机 Ollama），验证链路在真实模型下的行为。

- 自动探测模型：OLLAMA_MODEL 指定，否则从 /api/tags 选第一个含 "4b" 的，
  再不行取第一个；
- 自起被测服务（独立端口/数据目录），跑完自动停掉；
- 覆盖：健康/配置、简单直答、复杂任务异步、prefer_tier、进度附注、多轮召回、
  三协议（chat / chat-completions final+instant+流式 / responses 非流+续接+流式）、
  错误路径、并发、热参数、配置管理、真实重启、上下文压缩、控制台页面。

用法: python3 tests/test_real_model_smoke.py
前置: 本机 Ollama 已运行且至少有一个可用模型（无则先 `ollama pull gemma3:4b`）。
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
# ollama provider 走原生 /api/chat，base_url 不带 /v1
OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_TAGS = OLLAMA_BASE + "/api/tags"
API_PORT = int(os.getenv("TEST_API_PORT", "18951"))
BASE = f"http://127.0.0.1:{API_PORT}"

RESULTS = []


def check(name, ok, detail="", warn=False):
    RESULTS.append({"case": name, "ok": bool(ok), "detail": str(detail)[:300],
                    "warn": bool(warn)})
    tag = "PASS" if ok else ("WARN" if warn else "FAIL")
    print(f"[{tag}] {name}: {str(detail)[:220]}")


def http(method, url, body=None, timeout=180, raw=False):
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
            return {"_status": e.code, "_raw": payload[:300]}


def poll_task(task_id, timeout=300):
    deadline = time.time() + timeout
    t = {}
    while time.time() < deadline:
        t = http("GET", f"{BASE}/task_result?task_id={task_id}")
        if t.get("task_status") in ("completed", "failed"):
            return t
        time.sleep(0.8)
    return t


def write_fixture(tmp, model):
    import yaml
    cfg = {
        "runtime": {
            "server_bin": "llama-server", "lib_dir": "",
            "gpu_backend": "auto", "n_gpu_layers": 99,
            "max_output_tokens": 1024, "queue_maxsize": 32,
            "ports": {"api": API_PORT, "front": API_PORT + 10,
                      "slot": API_PORT + 11},
            "startup_timeout_s": 120, "request_timeout_s": 300,
        },
        "context": {"num_ctx": 32768, "kv_cache": "q8_0",
                    "compaction": {"enabled": True, "max_summary_chars": 800,
                                   "min_dropped_chars": 600}},
        "embedding": {"onnx": "models/embedding/model.onnx",
                      "tokenizer": "models/embedding/tokenizer.json"},
        "budget": {"reserve_system_mb": 1024, "headroom_ratio": 0.10},
        "slots": {
            "front": {"type": "api", "provider": "ollama",
                      "base_url": OLLAMA_BASE,
                      "model": model, "context_window": 32768,
                      "max_output_tokens": 2048,
                      "role": "front", "resident": True},
            "mid": {"type": "api", "provider": "ollama",
                    "base_url": OLLAMA_BASE,
                    "model": model, "context_window": 32768,
                    "max_output_tokens": 2048, "role": "middle"},
            "cloud": {"type": "api", "provider": "ollama",
                      "base_url": OLLAMA_BASE,
                      "model": model, "context_window": 32768,
                      "max_output_tokens": 2048, "role": "heavy"},
        },
        "routing": {
            "tier_order": ["simple", "mid", "heavy"],
            "tier_to_slot": {"simple": "front", "mid": "mid", "heavy": "cloud"},
            "tier_thresholds": {"mid": 25, "heavy": 120},
            "default_slot": "mid",
            "mid_min_chars": 25, "heavy_min_chars": 120,
            "max_input_chars": "auto",
            "complex_words": ["详细", "分析", "方案"],
        },
        "openai": {"default_mode": "final", "wait_timeout_s": 240},
        "frontend": {
            "max_tokens": 256, "history_budget_ratio": 0.45,
            "history_min_chars": 2048, "history_max_messages": 40,
            "context_safety_tokens": 256, "restart_cooldown_s": 30,
            "progress_window_s": 1800,
            "system_prompt": "你是本地助手，回答简洁准确。",
        },
    }
    path = pathlib.Path(tmp) / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    (pathlib.Path(tmp) / "settings.local.yaml").write_text("", encoding="utf-8")
    return path


def detect_model():
    """优先 OLLAMA_MODEL；否则 /api/tags 里选第一个含 4b 的，再取第一个。"""
    want = os.getenv("OLLAMA_MODEL")
    try:
        tags = json.load(urllib.request.urlopen(OLLAMA_TAGS, timeout=5))
        names = [m.get("name") or m.get("model") for m in tags.get("models") or []]
    except Exception as exc:
        raise SystemExit(f"Ollama 不可达（{OLLAMA_TAGS}）: {exc}\n"
                         f"请先启动 Ollama 并拉取模型，如: ollama pull gemma3:4b")
    if want:
        if want not in names:
            raise SystemExit(f"OLLAMA_MODEL={want} 不在已装模型里: {names}")
        return want, names
    for n in names:
        if "4b" in n.lower():
            return n, names
    if names:
        return names[0], names
    raise SystemExit("Ollama 没有任何已装模型。先执行: ollama pull gemma3:4b")


def main():
    import shutil
    model, all_names = detect_model()
    print(f"[smoke] Ollama 已装模型: {all_names} -> 使用 {model}")
    # 预检：直接对 Ollama 原生端点发一次真实请求，确认模型可推理
    pre = http("POST", OLLAMA_BASE + "/api/chat",
               {"model": model, "stream": False, "think": False,
                "messages": [{"role": "user", "content": "回复 OK"}]}, timeout=180)
    if pre.get("_status") or not ((pre.get("message") or {}).get("content")):
        raise SystemExit(f"Ollama 直接推理失败: {str(pre)[:300]}")
    print(f"[smoke] Ollama 直连推理 OK: "
          f"{(pre['message'].get('content') or '')[:40]!r}")

    tmp = tempfile.mkdtemp(prefix="explore-real-")
    svc = None
    try:
        settings_path = write_fixture(tmp, model)
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
        svc = subprocess.Popen(
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
                encoding="utf-8", errors="replace")[-1500:])
            raise SystemExit("服务未就绪")
        check("01_服务启动_前台就绪", True, f"api 前台 -> Ollama/{model}")

        # ---- 健康与配置 ----
        h = http("GET", f"{BASE}/health")
        c = h.get("config") or {}
        slots = {k: v.get("type") for k, v in (c.get("slots") or {}).items()}
        check("02_配置_三槽位全部指向ollama",
              slots == {"front": "api", "mid": "api", "cloud": "api"}
              and c.get("front_type") == "api",
              {"slots": slots})
        check("03_控制台页面", "explore" in http("GET", f"{BASE}/console", raw=True),
              "GET /console 200")

        # ---- 简单任务：直答 + 答案正确 ----
        r = http("POST", f"{BASE}/chat", body={
            "user_input": "1+1等于几？只回答数字。", "session_id": "smoke"})
        inst = r.get("instant_reply") or ""
        check("04_简单任务_前台直答",
              r.get("process_mode") == "direct" and r.get("task_status") == "completed"
              and bool(inst.strip()),
              {"mode": r.get("process_mode"), "instant": inst[:60]})
        check("05_答案内容正确", "2" in inst, inst[:80])

        # ---- 复杂任务：异步 + 结果原样递回 ----
        long_ask = ("请详细分析一个函数 f(x)=x*x 在区间 [0,2] 上的定积分该如何计算，"
                    "给出步骤说明。") + "背景补充。" * 20
        r = http("POST", f"{BASE}/chat", body={
            "user_input": long_ask, "session_id": "smoke", "prefer_tier": "heavy"})
        check("06_复杂任务_转后台异步",
              r.get("process_mode") == "async" and bool(r.get("task_id")),
              {"mode": r.get("process_mode"), "tier": r.get("task_type")})
        tid = r.get("task_id")
        t = poll_task(tid)
        final = t.get("final_reply") or ""
        check("07_后台任务完成_结果非空",
              t.get("task_status") == "completed" and len(final) > 20,
              {"status": t.get("task_status"), "model": t.get("chosen_model"),
               "len": len(final)})
        check("08_结果原样递回_无前缀污染",
              final and all(m not in final for m in
                            ("[后台结果]", "[后台任务进度]", "[后台任务失败]")),
              final[:60])
        check("09_final与instant不同_槽位为后台",
              final != (t.get("instant_reply") or "")
              and t.get("chosen_model") in ("mid", "cloud"),
              {"model": t.get("chosen_model")})

        # ---- prefer_tier 与档位路由 ----
        r = http("POST", f"{BASE}/chat", body={
            "user_input": "短任务", "session_id": "smoke", "prefer_tier": "heavy"})
        t = poll_task(r.get("task_id"))
        check("10_prefer_tier_heavy_路由生效",
              r.get("task_type") == "heavy" and t.get("chosen_model") == "cloud",
              {"tier": r.get("task_type"), "model": t.get("chosen_model")})

        # ---- 进度附注：任务运行中提问 ----
        r = http("POST", f"{BASE}/chat", body={
            "user_input": "请详细分析并给出一份完整方案：" + "素材" * 1500,
            "session_id": "smoke"})
        tid = r.get("task_id")
        r2 = http("POST", f"{BASE}/chat", body={
            "user_input": "刚才的进度怎么样了？", "session_id": "smoke"})
        poll_task(tid)
        check("11_进度提问_附真实状态",
              "[后台任务进度]" in (r2.get("instant_reply") or ""),
              (r2.get("instant_reply") or "")[:80])

        # ---- 多轮记忆：暗号召回（真实 4B 能力）----
        s = "recall-" + os.urandom(3).hex()
        http("POST", f"{BASE}/chat", body={
            "user_input": "请记住暗号 REM-OL-7Q2K，只回复收到。", "session_id": s})
        r = http("POST", f"{BASE}/chat", body={
            "user_input": "我刚才说的暗号是什么？只回复暗号本身。",
            "session_id": s})
        recall_ok = "REM-OL-7Q2K" in (r.get("instant_reply") or "")
        check("12_多轮暗号召回_真实模型", recall_ok,
              (r.get("instant_reply") or "")[:80], warn=True)

        # ---- OpenAI 兼容层 ----
        models = [m.get("id") for m in
                  (http("GET", f"{BASE}/v1/models").get("data") or [])]
        check("13_v1models_虚拟模型名",
              all(m in models for m in
                  ("explore", "explore-fast", "explore-simple",
                   "explore-mid", "explore-heavy")),
              models)
        ask = "请详细分析：为什么天是蓝的？给出三点原因。" + "。" * 130
        r = http("POST", f"{BASE}/v1/chat/completions", body={
            "model": "explore",
            "messages": [{"role": "user", "content": ask}]})
        meta = r.get("explore") or {}
        content = ((r.get("choices") or [{}])[0].get("message") or {}).get("content")
        t = poll_task(meta.get("task_id")) if meta.get("task_id") else {}
        check("14_openai_final_等后台完成",
              meta.get("process_mode") == "async" and bool(content)
              and content == (t.get("final_reply") or ""),
              {"content_len": len(content or ""),
               "final_len": len(t.get("final_reply") or "")})
        t0 = time.time()
        r = http("POST", f"{BASE}/v1/chat/completions", body={
            "model": "explore-fast",
            "messages": [{"role": "user", "content": ask}]})
        meta = r.get("explore") or {}
        check("15_openai_instant_立即返回",
              meta.get("process_mode") == "async" and meta.get("task_id")
              and time.time() - t0 < 30,
              {"elapsed_s": round(time.time() - t0, 1)})
        raw = http("POST", f"{BASE}/v1/chat/completions", body={
            "model": "explore", "stream": True,
            "messages": [{"role": "user", "content": "用一句话介绍自己"}]},
            raw=True, timeout=120)
        check("16_openai_stream_SSE分块与DONE",
              raw.startswith("data: ") and "data: [DONE]" in raw
              and "finish_reason" in raw,
              raw[:80])

        # ---- Responses API ----
        r1 = http("POST", f"{BASE}/v1/responses", body={
            "model": "explore", "input": "请记住暗号 RESP-OL-9M3X 并确认。",
            "store": True}, timeout=180)
        resp_id = r1.get("id")
        check("17_responses_非流式completed",
              str(resp_id).startswith("resp_") and r1.get("status") == "completed"
              and any(p.get("text") for item in (r1.get("output") or [])
                      for p in (item.get("content") or [])
                      if p.get("type") == "output_text"),
              {"id": resp_id, "status": r1.get("status")})
        r2 = http("POST", f"{BASE}/v1/responses", body={
            "model": "explore", "input": "刚才的暗号是什么？只回复暗号。",
            "previous_response_id": resp_id}, timeout=180)
        out2 = "".join(p.get("text") or "" for item in (r2.get("output") or [])
                       for p in (item.get("content") or [])
                       if p.get("type") == "output_text")
        check("18_responses_previous_id_续接",
              r2.get("status") == "completed"
              and (r2.get("explore") or {}).get("history_messages", 0) >= 2,
              {"hist": (r2.get("explore") or {}).get("history_messages"),
               "out2": out2[:60]})
        recall2 = "RESP-OL-9M3X" in out2
        check("19_responses_续接召回_真实模型", recall2, out2[:80], warn=True)
        raw = http("POST", f"{BASE}/v1/responses", body={
            "model": "explore", "input": "用一句话介绍explore", "stream": True},
            raw=True, timeout=120)
        check("20_responses_stream_事件序列无DONE",
              raw.startswith("event: ") and "response.completed" in raw
              and "[DONE]" not in raw, raw[:60])

        # ---- 错误路径 ----
        r = http("POST", f"{BASE}/chat", body={"user_input": ""})
        check("21_空输入_400", r.get("_status") == 400, r.get("_status"))
        r = http("POST", f"{BASE}/chat", body={"user_input": "x" * 50000})
        check("22_超长输入_413", r.get("_status") == 413, r.get("_status"))
        r = http("POST", f"{BASE}/chat",
                 body={"user_input": "hi", "prefer_tier": "notexist"})
        check("23_非法prefer_tier_400", r.get("_status") == 400, r.get("_status"))
        r = http("GET", f"{BASE}/task_result?task_id=not-exist")
        check("24_未知任务_404", r.get("code") == 404, r.get("code"))

        # ---- 并发 ----
        import concurrent.futures
        def _one(i):
            return http("POST", f"{BASE}/chat", body={
                "user_input": f"{i}+{i}等于几？只回答数字。",
                "session_id": f"conc-{i}"})
        with concurrent.futures.ThreadPoolExecutor(3) as ex:
            rs = list(ex.map(_one, range(3)))
        check("25_并发3路_全部成功",
              all(x.get("code") == 200 for x in rs)
              and all((x.get("instant_reply") or "").strip() for x in rs),
              [x.get("process_mode") for x in rs])

        # ---- 热参数与配置管理（真实服务上）----
        req = urllib.request.Request(
            BASE + "/admin/config",
            data=json.dumps({"FRONT_MAX_TOKENS": 180}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        json.load(urllib.request.urlopen(req, timeout=30))
        chat = http("POST", f"{BASE}/chat",
                    body={"user_input": "hi", "session_id": "smoke"})
        check("26_热参数_立即生效",
              (chat.get("context_usage") or {}).get("front_max_tokens") == 180,
              (chat.get("context_usage") or {}).get("front_max_tokens"))
        req = urllib.request.Request(BASE + "/admin/config/FRONT_MAX_TOKENS",
                                     method="DELETE")
        urllib.request.urlopen(req, timeout=30)

        r = http("POST", f"{BASE}/admin/settings", {"updates": {
            "routing.tier_fallbacks": {"heavy": ["cloud"]}}})
        saved_ok = "_status" not in r
        # 覆盖层写入即时可查（无需重启）：查覆盖层视图
        adm = http("GET", f"{BASE}/admin/config")
        check("27_配置管理_覆盖层落盘",
              saved_ok and bool(adm.get("overlay_loaded")),
              {"saved": saved_ok, "overlay": adm.get("overlay_file")})
        r = http("POST", f"{BASE}/admin/settings",
                 {"resets": ["routing.tier_fallbacks"]})
        check("28_配置管理_移除覆盖恢复", "_status" not in r, r.get("_status"))

        # ---- 真实重启：新实例重连 Ollama ----
        h0 = http("GET", f"{BASE}/health")
        prev = h0.get("started_at")
        req = urllib.request.Request(BASE + "/admin/restart", data=b"{}",
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        json.load(urllib.request.urlopen(req, timeout=30))
        deadline = time.time() + 150
        ok_restart = False
        while time.time() < deadline:
            try:
                h1 = http("GET", f"{BASE}/health", timeout=5)
                if (h1.get("code") == 200 and h1.get("started_at") != prev
                        and h1.get("frontend") == "ready"):
                    ok_restart = True
                    break
            except Exception:
                pass
            time.sleep(1.5)
        check("29_真实重启_新实例重连ollama", ok_restart,
              f"started_at {prev} -> new")

        # ---- 上下文压缩（真实模型摘要）----
        s = "compact-" + os.urandom(3).hex()
        marker = "REM-CP-A1"
        compressed = truncated = False
        for i in range(3):
            body_txt = f"第{i+1}段材料。请记住本段暗号 {marker}。" if i == 0 else f"第{i+1}段材料。"
            r = http("POST", f"{BASE}/chat", body={
                "user_input": body_txt + "这是一段用于压上下文预算的冗长材料。" * 420,
                "session_id": s}, timeout=300)
            cu = r.get("context_usage") or {}
            compressed = compressed or cu.get("context_compressed")
            truncated = truncated or cu.get("history_truncated")
        r = http("POST", f"{BASE}/chat", body={
            "user_input": "最开始的暗号是什么？只回复暗号。", "session_id": s},
            timeout=300)
        recall3 = marker in (r.get("instant_reply") or "")
        check("30_上下文压缩_真实模型摘要",
              compressed, {"compressed": compressed, "truncated": truncated},
              warn=True)
        check("31_压缩后暗号召回", recall3,
              (r.get("instant_reply") or "")[:80], warn=True)

    finally:
        if svc:
            try:
                svc.terminate()
                svc.wait(timeout=15)
            except Exception:
                svc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for x in RESULTS if x["ok"])
    warns = sum(1 for x in RESULTS if not x["ok"] and x.get("warn"))
    print(f"\n=== REAL_MODEL_SMOKE: {passed}/{len(RESULTS)} 通过"
          f"（{warns} 项模型能力相关按 WARN 口径记录） ===")
    return 1 if any(not x["ok"] and not x.get("warn") for x in RESULTS) else 0


if __name__ == "__main__":
    raise SystemExit(main())

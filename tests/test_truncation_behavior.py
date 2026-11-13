"""截断行为实证：验证「强制截断上下文」各路径的可观测性与信息丢失边界。

场景（全部真实推理，Ollama qwen3.5:4b）：
A1 后台槽位小窗口（cloud context_window=2048）+ 超长输入 → 任务包中间截断：
   必须可观测（release_info 含「输入已截断」），任务不失败；
A2 同一输入走默认 32K 窗口 → 不触发截断（release_info 无截断标记），中间不丢；
A3 前台路径超长输入 → 413 拒绝（绝不静默截断）；
A4 输出撞 max_tokens → finish_reason=length 透传（不谎称完成）。
用法: OLLAMA_MODEL=qwen3.5:4b python3 tests/test_truncation_behavior.py
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
OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
API_PORT = int(os.getenv("TEST_API_PORT", "18961"))
BASE = f"http://127.0.0.1:{API_PORT}"
MARK_H, MARK_M, MARK_T = "HEAD-KEY-111", "MID-KEY-222", "TAIL-KEY-333"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:220]}")


def http(method, url, body=None, timeout=300, raw=False):
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
        try:
            return {"_status": e.code, **json.loads(payload)}
        except Exception:
            return {"_status": e.code, "_raw": payload[:200]}


def poll_task(task_id, timeout=300):
    deadline = time.time() + timeout
    t = {}
    while time.time() < deadline:
        t = http("GET", f"{BASE}/task_result?task_id={task_id}")
        if t.get("task_status") in ("completed", "failed"):
            return t
        time.sleep(0.8)
    return t


def write_fixture(tmp, cloud_ctx):
    import yaml
    slot = {"type": "api", "provider": "ollama", "base_url": OLLAMA_BASE,
            "model": MODEL, "context_window": cloud_ctx,
            "max_output_tokens": 1024, "role": "heavy"}
    cfg = {
        "runtime": {"server_bin": "llama-server", "lib_dir": "",
                    "gpu_backend": "auto", "n_gpu_layers": 99,
                    "max_output_tokens": 1024, "queue_maxsize": 32,
                    "ports": {"api": API_PORT, "front": API_PORT + 10,
                              "slot": API_PORT + 11},
                    "startup_timeout_s": 120, "request_timeout_s": 300},
        "context": {"num_ctx": 32768, "kv_cache": "q8_0",
                    "compaction": {"enabled": True, "max_summary_chars": 800,
                                   "min_dropped_chars": 600}},
        "embedding": {"provider": "ollama", "ollama_url": OLLAMA_BASE,
                      "ollama_model": "bge-m3"},
        "budget": {"reserve_system_mb": 1024, "headroom_ratio": 0.10},
        "slots": {
            "front": {"type": "api", "provider": "ollama", "base_url": OLLAMA_BASE,
                      "model": MODEL, "context_window": 32768,
                      "max_output_tokens": 256, "role": "front", "resident": True},
            "mid": {"type": "api", "provider": "ollama", "base_url": OLLAMA_BASE,
                    "model": MODEL, "context_window": 32768,
                    "max_output_tokens": 1024, "role": "middle"},
            "cloud": slot,
        },
        "routing": {"tier_order": ["simple", "mid", "heavy"],
                    "tier_to_slot": {"simple": "front", "mid": "mid",
                                     "heavy": "cloud"},
                    "tier_thresholds": {"mid": 25, "heavy": 120},
                    "default_slot": "mid", "mid_min_chars": 25,
                    "heavy_min_chars": 120, "max_input_chars": "auto",
                    "complex_words": ["详细", "分析"]},
        "openai": {"default_mode": "final", "wait_timeout_s": 240},
        "frontend": {"max_tokens": 256, "history_budget_ratio": 0.45,
                     "history_min_chars": 2048, "history_max_messages": 40,
                     "context_safety_tokens": 256, "restart_cooldown_s": 30,
                     "progress_window_s": 1800,
                     "system_prompt": "你是本地助手，回答简洁准确。"},
    }
    path = pathlib.Path(tmp) / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    (pathlib.Path(tmp) / "settings.local.yaml").write_text("", encoding="utf-8")
    return path


def run_unit_tests():
    """截断函数单元测试（离线，无服务/模型依赖）。"""
    sys.path.insert(0, str(ROOT))
    from app import prompts

    MARK = prompts.TRUNCATION_MARK.strip()
    seg = "这是填充段落，用于撑长输入测试截断行为。" * 40

    # U1 空行分段：头尾保留完整单元（超长段落允许句子级细分），切点全部在句界
    text = (f"开头段 {MARK_H}。\n\n" + seg + f"\n\n居中段 {MARK_M}。\n\n"
            + seg + f"\n\n结尾段 {MARK_T}。")
    out, trunc = prompts.truncate_for_task(text, 900)
    head_part, tail_part = out.split(MARK)[0], out.split(MARK)[1]
    check("U1_段落边界_头尾完整切点在句界",
          trunc and MARK in out
          and MARK_H in head_part and MARK_T in tail_part
          and head_part.rstrip().endswith(("。", "！", "？", "；"))
          and "共省略" in out,
          {"truncated": trunc, "head_tail": (out[:60], out[-60:])})

    # U2 无换行连续文本：切点落在句子边界（保留片段以句末标点收尾）
    text2 = (f"第一句出现 {MARK_H}。" + seg + f"中间句出现 {MARK_M}。"
             + seg + f"最后一句出现 {MARK_T}。")
    out2, trunc2 = prompts.truncate_for_task(text2, 800)
    head_part = out2.split(MARK)[0]
    check("U2_句界切点_半句不丢",
          trunc2 and head_part.rstrip().endswith(("。", "！", "？", "；"))
          and MARK_H in out2.split(MARK)[0] and MARK_T in out2.split(MARK)[1],
          {"head_tail": (out2.split(MARK)[0][-40:], out2.split(MARK)[1][:40])})

    # U3 决定性断言：HEAD/TAIL 保留，MID 落入被省略的中间
    check("U3_关键标记分布_头尾保留中间省略",
          MARK_M not in out2.split(MARK)[0] and MARK_M not in out2.split(MARK)[1],
          {"mid_head": MARK_M in out2.split(MARK)[0],
           "mid_tail": MARK_M in out2.split(MARK)[1]})

    # U4 预算内不截断
    out4, trunc4 = prompts.truncate_for_task("短文本 " + seg, 100000)
    check("U4_预算内_原样保留", not trunc4 and out4.endswith("。"), trunc4)

    # U5 超大单段：句子级细分兜底（仍不切断半句）
    huge = ("只有一段没有任何换行的超长文本。" * 80) + f"末尾标记 {MARK_T}。"
    out5, trunc5 = prompts.truncate_for_task(huge, 700)
    head5 = out5.split(MARK)[0]
    check("U5_超大单段_句界细分",
          trunc5 and head5.rstrip().endswith(("。", "！", "？", "；")),
          {"truncated": trunc5, "head_tail_40": head5[-40:]})


def main():
    run_unit_tests()
    import shutil
    tmp = tempfile.mkdtemp(prefix="explore-trunc-")
    svc = None
    try:
        settings_path = write_fixture(tmp, cloud_ctx=2048)
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
                encoding="utf-8", errors="replace")[-1200:])
            raise SystemExit("服务未就绪")

        # 带三个「关键标记」的超长输入：首/中/尾各一个，验证截断保留策略
        seg = "这是填充材料段落，用于撑长输入。" * 40          # ~660 字
        long_text = (f"关键标记开头 {MARK_H}。" + seg
                     + f"关键标记居中 {MARK_M}。" + seg
                     + f"关键标记结尾 {MARK_T}。"
                     + "请详细分析以上三段材料并分别复述其中出现的关键标记。")

        # A1: 后台小窗口（2048）→ 任务包截断（可观测）
        r = http("POST", f"{BASE}/chat", body={
            "user_input": long_text, "session_id": "trunc",
            "prefer_tier": "heavy"})
        tid = r.get("task_id")
        t = poll_task(tid)
        release = t.get("release_info") or ""
        check("A1_小窗口任务包截断_可观测",
              t.get("task_status") == "completed" and "输入已截断" in release,
              {"status": t.get("task_status"), "release": release[:80]})

        # A2: 同输入走前台即时路径（32K 窗口）→ 输入完整，三个标记都在
        r2 = http("POST", f"{BASE}/chat", body={
            "user_input": long_text, "session_id": "trunc-front",
            "prefer_tier": "simple"})
        inst = r2.get("instant_reply") or ""
        seen = {m: (m in inst) for m in (MARK_H, MARK_M, MARK_T)}
        check("A2_前台32K窗口_输入不被截断",
              r2.get("process_mode") == "direct" and all(seen.values()),
              {"mode": r2.get("process_mode"), "seen": seen,
               "answer": inst[:60]})

        # A3: 前台路径超上限 → 413，绝不静默截断
        huge = "x" * 50000
        r3 = http("POST", f"{BASE}/chat",
                  body={"user_input": huge, "session_id": "trunc-413"})
        check("A3_超上限输入_413拒绝", r3.get("_status") == 413, r3.get("_status"))

        # A4: 输出撞 max_tokens → finish_reason=length 透传
        r4 = http("POST", f"{BASE}/chat", body={
            "user_input": "请详细分析并写一份不少于八百字的报告：" + "报告素材。" * 30,
            "session_id": "trunc-out", "prefer_tier": "heavy"})
        t4 = poll_task(r4.get("task_id"))
        finish = t4.get("finish_reason")
        check("A4_输出截断_finish_reason透传",
              t4.get("task_status") == "completed" and finish in ("length", "stop"),
              {"finish": finish, "final_len": len(t4.get("final_reply") or "")})

        # B: 历史截断可观测（历史超预算 → history_truncated 标记）
        s = "hist-" + os.urandom(3).hex()
        seen_trunc = False
        for i in range(4):
            r5 = http("POST", f"{BASE}/chat", body={
                "user_input": ("请记住本轮标记 HIST-%d。" % i)
                + "长历史填充材料。" * 1500,
                "session_id": s}, timeout=300)
            cu = r5.get("context_usage") or {}
            seen_trunc = seen_trunc or cu.get("history_truncated")
        check("B_历史超预算_截断可观测(history_truncated)",
              seen_trunc, {"history_truncated": seen_trunc})

    finally:
        if svc:
            try:
                svc.terminate()
                svc.wait(timeout=15)
            except Exception:
                svc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n=== TRUNCATION_BEHAVIOR: {passed}/{len(RESULTS)} ===")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

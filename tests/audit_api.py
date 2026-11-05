"""真机功能审计：API 契约、错误路径、真实内容、并发、队列、会话与多轮。

不使用 mock：所有断言都打真实服务、真实模型。内容类断言分两档：
- hard：链路/契约问题，失败即审计不通过；
- warn：模型质量类问题（如小模型回复过短），记录但不掩盖链路可用性。
"""
import json
import os
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = os.getenv("BASE", f"http://127.0.0.1:{os.getenv('API_PORT', '8100')}")
ROOT = pathlib.Path(__file__).resolve().parent.parent
TIMEOUT = float(os.getenv("AUDIT_TIMEOUT", "600"))
RESULTS = []


def check(name, ok, detail, severity="hard"):
    RESULTS.append({"case": name, "ok": bool(ok), "severity": severity,
                    "detail": str(detail)[:600]})
    print(f"[{'PASS' if ok else 'FAIL'}] ({severity}) {name}: {str(detail)[:220]}",
          flush=True)


def health():
    return httpx.get(BASE + "/health", timeout=30).json()


def cfg():
    return httpx.get(BASE + "/config", timeout=30).json()


def post_chat(text, sid, prefer=None, timeout=120):
    body = {"user_input": text, "session_id": sid}
    if prefer:
        body["prefer_tier"] = prefer
    return httpx.post(BASE + "/chat", json=body, timeout=timeout)


def wait(task_id, timeout=TIMEOUT):
    deadline = time.time() + timeout
    seq = []
    while time.time() < deadline:
        d = httpx.get(BASE + "/task_result",
                      params={"task_id": task_id}, timeout=20).json()
        st = d.get("task_status")
        if not seq or seq[-1] != st:
            seq.append(st)
        if st in ("completed", "failed"):
            d["_status_seq"] = seq
            return d
        time.sleep(2)
    return {"task_status": "timeout", "_status_seq": seq}


def main():
    sid = f"audit-{int(time.time())}"
    c = httpx.Client(base_url=BASE, timeout=60)

    # ---------- 1. 契约 ----------
    h = health()
    conf = cfg()
    tier_order = conf["tier_order"]
    slot_map = conf["tier_to_slot"]
    front_slot = conf["front_slot"]
    residents = [k for k, v in conf["slots"].items() if v["resident"]]
    backend_slots = [k for k in conf["slots"] if k not in residents]

    check("health_contract",
          h.get("status") == "success" and h.get("backend") in ("cuda", "rocm", "cpu")
          and isinstance(h.get("num_ctx"), int) and h["num_ctx"] > 0
          and h.get("frontend") == "ready"
          and isinstance(h.get("slot"), dict)
          and h.get("embedding", {}).get("enabled") is True
          and h.get("embedding", {}).get("dim", 0) > 0
          and all(k in h.get("vram", {}) for k in ("total_mb", "used_mb", "free_mb")),
          f"backend={h.get('backend')} ctx={h.get('num_ctx')} "
          f"front={h.get('frontend')} embed={h.get('embedding')}")

    check("config_contract",
          len(tier_order) == 3 and len(residents) == 1 and front_slot == residents[0]
          and all(t in slot_map for t in tier_order)
          and all(s in conf["slots"] for s in slot_map.values())
          and conf.get("limits", {}).get("max_input_chars", 0) > 0
          and conf.get("limits", {}).get("queue_maxsize", -1) >= 0,
          f"tiers={tier_order} map={slot_map} residents={residents} "
          f"limits={conf.get('limits')}")

    g = httpx.get(BASE + "/gpu", timeout=30).json()
    check("gpu_endpoint", g.get("backend") == h.get("backend")
          and isinstance(g.get("gpus"), list),
          f"backend={g.get('backend')} gpus={len(g.get('gpus', []))}")

    # ---------- 2. 错误路径 ----------
    r = c.post("/chat", json={"user_input": "  "})
    check("empty_input_400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

    max_chars = conf["limits"]["max_input_chars"]
    r = c.post("/chat", json={"user_input": "啊" * (max_chars + 1)})
    check("too_long_413", r.status_code == 413,
          f"{r.status_code} len={max_chars + 1} {r.text[:120]}")

    r = c.post("/chat", json={"user_input": "你好", "prefer_tier": "not-a-tier"})
    check("bad_prefer_tier_400", r.status_code == 400,
          f"{r.status_code} {r.text[:120]}")

    r = c.get("/task_result", params={"task_id": "does-not-exist"})
    check("unknown_task_404", r.status_code == 200 and r.json().get("code") == 404,
          f"{r.status_code} {r.text[:120]}")

    r = c.get("/tasks", params={"limit": 0})
    check("tasks_limit_validation", r.status_code == 422, str(r.status_code))

    # ---------- 3. 简单任务：真实内容 ----------
    r1 = post_chat("只回答一个数字：2+2 等于几？", sid).json()
    ok_direct = r1.get("process_mode") == "direct"
    check("simple_direct", ok_direct and bool(r1.get("instant_reply")),
          f"mode={r1.get('process_mode')} reply={str(r1.get('instant_reply'))[:80]}")
    check("simple_content_math", "4" in str(r1.get("instant_reply", "")),
          f"reply={str(r1.get('instant_reply'))[:120]}", severity="warn")

    d1 = wait(r1["task_id"], timeout=60)
    check("simple_task_completed",
          d1.get("task_status") == "completed" and d1.get("final_reply"),
          f"status={d1.get('task_status')} final={str(d1.get('final_reply'))[:80]}")

    qurl = r1.get("query_url", "")
    qr = httpx.get(qurl, timeout=30) if qurl else None
    check("query_url_reachable",
          bool(qr) and qr.status_code == 200
          and qr.json().get("task_id") == r1["task_id"],
          f"url={qurl} status={qr.status_code if qr else None}")

    # ---------- 4. 中档任务：真实内容 ----------
    r2 = post_chat("用一句话说明什么是本地大模型路由", sid, "mid").json()
    check("mid_async", r2.get("process_mode") == "async"
          and r2.get("task_type") == "mid"
          and slot_map.get("mid") in conf["slots"],
          f"mode={r2.get('process_mode')} tier={r2.get('task_type')} "
          f"qpos={r2.get('queue_position')}")
    d2 = wait(r2["task_id"])
    reply2 = str(d2.get("final_reply") or "")
    check("mid_completed", d2.get("task_status") == "completed" and len(reply2) >= 5,
          f"status={d2.get('task_status')} len={len(reply2)} "
          f"slot={d2.get('chosen_model')} err={d2.get('error_msg')}")
    check("mid_content_relevant",
          any(k in reply2 for k in ("模型", "路由", "本地", "local", "model")),
          reply2[:160], severity="warn")
    check("mid_model_selected",
          d2.get("chosen_model") == slot_map.get("mid"),
          f"chosen={d2.get('chosen_model')} expected={slot_map.get('mid')}")

    # ---------- 5. 重档任务：真实内容 + 结果原样递回 ----------
    r3 = post_chat("列出三种常见的排序算法，并各用一句话说明", sid, "heavy").json()
    check("heavy_async", r3.get("process_mode") == "async"
          and r3.get("task_type") == "heavy",
          f"mode={r3.get('process_mode')} tier={r3.get('task_type')}")

    # 任务进行中问进度（真实时序）
    time.sleep(1)
    r3p = post_chat("进度怎么样了？", sid).json()
    check("progress_during_task",
          "[后台任务进度]" in str(r3p.get("instant_reply", "")),
          f"reply={str(r3p.get('instant_reply'))[-160:]}")

    d3 = wait(r3["task_id"])
    reply3 = str(d3.get("final_reply") or "")
    check("heavy_completed", d3.get("task_status") == "completed" and len(reply3) >= 5,
          f"status={d3.get('task_status')} len={len(reply3)} "
          f"slot={d3.get('chosen_model')} err={d3.get('error_msg')}")
    check("heavy_answer_detail", len(reply3) >= 60,
          f"len={len(reply3)}（要求三种算法各一句，过短属于模型输出质量问题）",
          severity="warn")
    check("heavy_content_algorithms",
          "排序" in reply3 and any(k in reply3 for k in
                                   ("冒泡", "快速", "归并", "插入", "选择", "堆")),
          reply3[:200])
    check("heavy_model_selected",
          d3.get("chosen_model") == slot_map.get("heavy"),
          f"chosen={d3.get('chosen_model')} expected={slot_map.get('heavy')}")
    check("heavy_result_not_rewritten",
          reply3 and reply3 != str(r3.get("instant_reply") or ""),
          "final_reply 与前台即时回复不同，原样递回")
    check("prefill_metric",
          isinstance(d3.get("prefill_ms"), (int, float)) and d3["prefill_ms"] > 0
          and "tok/s" in str(d3.get("release_info", "")),
          f"prefill_ms={d3.get('prefill_ms')} release={d3.get('release_info')}")

    # ---------- 6. 队列：连续重档任务 ----------
    batch = [post_chat("请详细分析本地推理的显存优化方案，给出三点建议",
                       sid, "heavy").json() for _ in range(3)]
    time.sleep(1)
    qd = health().get("queue_depth")
    check("queue_visible", isinstance(qd, int) and qd >= 1,
          f"queue_depth={qd}（任务若已快速完成，深度可能为 0，仅作观察）",
          severity="warn")
    done = [wait(t["task_id"]) for t in batch]
    check("queue_drained",
          all(x.get("task_status") == "completed" for x in done)
          and health().get("queue_depth") == 0,
          f"statuses={[x.get('task_status') for x in done]} "
          f"queue={health().get('queue_depth')}")

    # ---------- 7. 并发简单请求 ----------
    def one(i):
        t0 = time.time()
        try:
            r = httpx.post(BASE + "/chat", json={
                "user_input": f"第{i}条：只回复 OK", "session_id": sid + "-conc",
            }, timeout=180)
            d = r.json()
            return {"i": i, "status": r.status_code, "mode": d.get("process_mode"),
                    "reply": bool(d.get("instant_reply")),
                    "ms": round((time.time() - t0) * 1000, 1)}
        except Exception as exc:
            return {"i": i, "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=10) as ex:
        conc = list(ex.map(one, range(10)))
    check("concurrent_simple_10",
          all(x.get("status") == 200 and x.get("reply") for x in conc),
          f"ok={sum(1 for x in conc if x.get('status') == 200)}/10 "
          f"max_ms={max((x.get('ms') or 0) for x in conc)} "
          f"errors={[x for x in conc if x.get('error')][:2]}")

    # ---------- 8. 会话隔离（存储层，不依赖模型记忆） ----------
    sa, sb = sid + "-A", sid + "-B"
    ra = post_chat("请写一段关于缓存设计的说明", sa, "mid").json()
    rb = post_chat("请写一段关于日志设计的说明", sb, "mid").json()
    da, db = wait(ra["task_id"]), wait(rb["task_id"])
    check("session_isolation",
          da.get("session_id") == sa and db.get("session_id") == sb
          and da.get("task_id") != db.get("task_id"),
          f"A={da.get('session_id')} B={db.get('session_id')}")

    # ---------- 9. 多轮记忆与后台结果回读（模型能力，warn） ----------
    sm = sid + "-mem"
    post_chat("请记住数字 7391，只回复收到", sm)
    rm = post_chat("我刚才让你记住的数字是多少？只回答数字", sm).json()
    check("multi_turn_memory",
          "7391" in str(rm.get("instant_reply", "")),
          f"reply={str(rm.get('instant_reply'))[:120]}", severity="warn")

    rq = post_chat("刚才后台任务列出的排序算法里，有哪些？简要回答", sid).json()
    check("backend_result_in_history",
          any(k in str(rq.get("instant_reply", "")) for k in
              ("冒泡", "快速", "归并", "插入", "选择", "堆", "排序")),
          f"reply={str(rq.get('instant_reply'))[:160]}", severity="warn")

    # ---------- 10. 上下文与状态一致性 ----------
    h2 = health()
    ctxs = {h.get("num_ctx"), conf.get("num_ctx"), r1.get("num_ctx"),
            d3.get("num_ctx"), h2.get("num_ctx")}
    check("num_ctx_consistent", len(ctxs) == 1, f"values={ctxs}")
    check("no_hanging_task", h2.get("queue_depth") == 0, f"queue={h2.get('queue_depth')}")
    check("slot_state_valid",
          h2.get("slot", {}).get("current") in conf["slots"]
          or h2.get("slot", {}).get("current") is None,
          str(h2.get("slot")))

    # ---------- 汇总 ----------
    hard = [x for x in RESULTS if x["severity"] == "hard"]
    warns = [x for x in RESULTS if x["severity"] == "warn"]
    report = {
        "base": BASE,
        "config": conf,
        "cases": RESULTS,
        "hard_total": len(hard),
        "hard_passed": sum(1 for x in hard if x["ok"]),
        "warn_total": len(warns),
        "warn_passed": sum(1 for x in warns if x["ok"]),
        "warnings": [x for x in warns if not x["ok"]],
        "all_pass": all(x["ok"] for x in hard),
        "finished_at": time.time(),
    }
    out = ROOT / "logs" / "audit_api.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== AUDIT SUMMARY ===")
    print(json.dumps({k: report[k] for k in
                      ("hard_total", "hard_passed", "warn_total", "warn_passed",
                       "all_pass")}, ensure_ascii=False))
    if report["warnings"]:
        print("WARNINGS:")
        for w in report["warnings"]:
            print(f" - {w['case']}: {w['detail'][:200]}")
    print(f"report -> {out}")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

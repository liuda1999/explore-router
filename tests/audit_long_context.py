"""长上下文与长文本专项审计：多轮累积、长输入、连续长回复、跨模型回读。

不使用 mock：全部打真实服务与真实模型。
长回复类断言分 hard（链路/不崩溃）与 warn（长度/质量）。
"""
import json
import os
import pathlib
import secrets
import time

import httpx

BASE = os.getenv("BASE", f"http://127.0.0.1:{os.getenv('API_PORT', '8100')}")
ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS = []


def check(name, ok, detail, severity="hard"):
    RESULTS.append({"case": name, "ok": bool(ok), "severity": severity,
                    "detail": str(detail)[:700]})
    print(f"[{'PASS' if ok else 'FAIL'}] ({severity}) {name}: {str(detail)[:260]}",
          flush=True)


def health():
    return httpx.get(BASE + "/health", timeout=30).json()


def post_chat(text, sid, prefer=None, timeout=600):
    body = {"user_input": text, "session_id": sid}
    if prefer:
        body["prefer_tier"] = prefer
    return httpx.post(BASE + "/chat", json=body, timeout=timeout)


def wait_task(task_id, timeout=600):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = httpx.get(BASE + "/task_result", params={"task_id": task_id},
                             timeout=20).json()
        except Exception:
            time.sleep(2)
            continue
        if last.get("task_status") in ("completed", "failed"):
            return last
        time.sleep(2)
    return last or {"task_status": "timeout"}


def filler(chars):
    unit = ("本地大模型部署需要关注显存占用、量化精度、上下文长度、批处理与并发、"
            "KV cache 预算、模型切换成本、预热策略、观测指标与回滚方案。")
    return (unit * (chars // len(unit) + 1))[:chars]


def snapshot(stage):
    h = health()
    return {"stage": stage, "num_ctx": h.get("num_ctx"),
            "vram_free_mb": h.get("vram", {}).get("free_mb"),
            "vram_used_mb": h.get("vram", {}).get("used_mb"),
            "queue": h.get("queue_depth"),
            "slot": h.get("slot", {}).get("current")}


def main():
    sid = f"longctx-{int(time.time())}"
    conf = httpx.get(BASE + "/config", timeout=30).json()
    ctx = conf["num_ctx"]
    snaps = [snapshot("initial")]
    check("initial_ready", health().get("frontend") == "ready",
          f"ctx={ctx}")

    # ---------- 1. 多轮长上下文：每轮约 1.2K 字，累积约 12K 字 ----------
    rounds = 10
    per_round = int(os.getenv("LONG_ROUND_CHARS", "1200"))
    # 每轮用随机标记，避免模型按 LONGCTX-{i} 规律“推断”而不是真正检索历史
    markers = {i: f"MARK-{i}-{secrets.token_hex(4).upper()}" for i in range(1, rounds + 1)}
    turn_ms = []
    turn_ok = 0
    turn_usage = []
    for i in range(1, rounds + 1):
        text = (f"第{i}轮资料（唯一标记 {markers[i]}）：{filler(per_round)}"
                f"\n请只回复：第{i}轮收到")
        t0 = time.time()
        r = post_chat(text, sid, timeout=600)
        turn_ms.append(round((time.time() - t0) * 1000, 1))
        d = r.json() if r.status_code == 200 else {}
        if r.status_code == 200 and d.get("instant_reply"):
            turn_ok += 1
        if d.get("context_usage"):
            turn_usage.append(d["context_usage"])
        if i % 5 == 0:
            snaps.append(snapshot(f"turn_{i}"))

    check("multi_turn_all_replied", turn_ok == rounds,
          f"{turn_ok}/{rounds}，单轮耗时 min={min(turn_ms)}ms "
          f"max={max(turn_ms)}ms avg={round(sum(turn_ms) / len(turn_ms), 1)}ms")

    # 自动阈值：任何一轮的估算总量都不能越过 num_ctx；历史预算应随轮次增长
    max_est = max((u.get("estimated_total_chars", 0) for u in turn_usage), default=0)
    check("context_budget_respected",
          bool(turn_usage) and all(
              u.get("estimated_total_chars", 0) <= ctx for u in turn_usage),
          f"max_estimated={max_est} ctx={ctx}")
    check("history_budget_auto",
          len(turn_usage) >= 2
          and turn_usage[-1].get("history_chars", 0) > turn_usage[0].get("history_chars", 0),
          f"first_history={turn_usage[0].get('history_chars') if turn_usage else None} "
          f"last_history={turn_usage[-1].get('history_chars') if turn_usage else None} "
          f"last_budget={turn_usage[-1].get('history_budget_chars') if turn_usage else None} "
          f"last_messages={turn_usage[-1].get('history_messages') if turn_usage else None}")

    # 最近窗口内（第 8 轮）应能召回；第 1 轮可能超出自动历史预算
    r8 = post_chat("请只回答第 8 轮资料里的唯一标记。", sid, timeout=300).json()
    check("recall_recent_turn",
          markers[8] in str(r8.get("instant_reply", "")),
          f"expect={markers[8]} reply={str(r8.get('instant_reply'))[:160]}")

    r1 = post_chat("请只回答第 1 轮资料里的唯一标记。", sid, timeout=300).json()
    check("recall_earliest_turn",
          markers[1] in str(r1.get("instant_reply", "")),
          f"expect={markers[1]} reply={str(r1.get('instant_reply'))[:160]}"
          f"（自动历史预算 ratio={conf['limits']['history_budget_ratio']}，"
          f"min_chars={conf['limits']['history_min_chars']}，超出预算属预期限制）",
          severity="warn")

    # ---------- 2. 单次长输入：默认 20K 字，可用 LONG_INPUT_CHARS 覆盖 ----------
    long_chars = int(os.getenv("LONG_INPUT_CHARS", "20000"))
    t0 = time.time()
    rl = post_chat(
        f"资料：{filler(long_chars)}\n\n请用一句话总结这段资料的主题。",
        sid + "-long", timeout=900)
    long_ms = round((time.time() - t0) * 1000, 1)
    dl = rl.json() if rl.status_code == 200 else {}
    check("long_input_accepted",
          rl.status_code == 200 and bool(dl.get("instant_reply")),
          f"status={rl.status_code} {long_ms}ms mode={dl.get('process_mode')} "
          f"reply={str(dl.get('instant_reply'))[:100]} "
          f"len_in={long_chars} max={conf['limits']['max_input_chars']}")
    if dl.get("context_usage"):
        check("long_input_budget_respected",
              dl["context_usage"].get("estimated_total_chars", 0) <= ctx,
              f"estimated={dl['context_usage'].get('estimated_total_chars')} ctx={ctx} "
              f"history_budget={dl['context_usage'].get('history_budget_chars')}")
    if dl.get("process_mode") == "async" and dl.get("task_id"):
        dd = wait_task(dl["task_id"], timeout=900)
        check("long_input_backend_completed",
              dd.get("task_status") == "completed"
              and len(str(dd.get("final_reply") or "")) >= 20,
              f"status={dd.get('task_status')} "
              f"chars={len(str(dd.get('final_reply') or ''))} "
              f"slot={dd.get('chosen_model')} err={dd.get('error_msg')}")
    else:
        check("long_input_backend_completed", True,
              "简单档直接完成，无需后台")
    snaps.append(snapshot("after_long_input"))

    # ---------- 3. 连续长回复：后台重档 3 次，每次要求约 900 字 ----------
    long_replies = []
    for i in range(3):
        r = post_chat(
            f"第{i + 1}篇：请写一篇约 900 字的本地大模型部署说明，"
            "分三个小节（显存、上下文、切换成本），内容要完整，不要只列标题。",
            sid + "-gen", "heavy", timeout=300).json()
        t0 = time.time()
        d = wait_task(r["task_id"], timeout=900)
        elapsed = round(time.time() - t0, 1)
        text = str(d.get("final_reply") or "")
        long_replies.append({
            "i": i + 1, "status": d.get("task_status"),
            "chars": len(text), "elapsed_s": elapsed,
            "slot": d.get("chosen_model"), "prefill_ms": d.get("prefill_ms"),
            "release": d.get("release_info"), "err": d.get("error_msg"),
        })
        check(f"long_reply_{i + 1}_completed",
              d.get("task_status") == "completed" and len(text) >= 100,
              f"chars={len(text)} elapsed={elapsed}s slot={d.get('chosen_model')} "
              f"err={d.get('error_msg')}")
        check(f"long_reply_{i + 1}_length",
              len(text) >= 600,
              f"chars={len(text)}（目标约 900 字；过短属模型/输出上限问题）",
              severity="warn")
        snaps.append(snapshot(f"long_reply_{i + 1}"))

    # ---------- 4. 长会话后触发后台任务，再回读结果 ----------
    rq = post_chat("请写一段关于缓存淘汰策略的说明", sid, "mid", timeout=300).json()
    dq = wait_task(rq["task_id"])
    rr = post_chat("刚才后台任务讲的主题是什么？用一句话回答。", sid, timeout=300).json()
    check("backend_after_long_session",
          dq.get("task_status") == "completed" and bool(dq.get("final_reply")),
          f"status={dq.get('task_status')} slot={dq.get('chosen_model')} "
          f"len={len(str(dq.get('final_reply') or ''))}")
    check("readback_after_long_session",
          bool(rr.get("instant_reply")),
          f"reply={str(rr.get('instant_reply'))[:160]}", severity="warn")

    # ---------- 5. 状态一致性 ----------
    h = health()
    snaps.append(snapshot("final"))
    check("ctx_unchanged", h.get("num_ctx") == ctx,
          f"{ctx} -> {h.get('num_ctx')}")
    check("no_queue_backlog", h.get("queue_depth") == 0, h.get("queue_depth"))
    check("vram_no_runaway",
          h.get("vram", {}).get("free_mb", 0) > 0,
          f"free={h.get('vram', {}).get('free_mb')}MB")

    hard = [x for x in RESULTS if x["severity"] == "hard"]
    warns = [x for x in RESULTS if x["severity"] == "warn"]
    report = {
        "base": BASE, "num_ctx": ctx, "rounds": rounds,
        "per_round_chars": per_round, "long_input_chars": long_chars,
        "turn_ms": turn_ms, "long_input_ms": long_ms,
        "long_replies": long_replies, "snapshots": snaps,
        "cases": RESULTS,
        "hard_total": len(hard), "hard_passed": sum(1 for x in hard if x["ok"]),
        "warn_total": len(warns), "warn_passed": sum(1 for x in warns if x["ok"]),
        "all_pass": all(x["ok"] for x in hard),
    }
    out = ROOT / "logs" / f"audit_long_context_{ctx}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== LONG CONTEXT SUMMARY ===")
    print(json.dumps({k: report[k] for k in
                      ("num_ctx", "hard_total", "hard_passed",
                       "warn_total", "warn_passed", "all_pass")},
                     ensure_ascii=False))
    print("long_replies:", json.dumps(long_replies, ensure_ascii=False))
    print(f"report -> {out}")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""主路快速检查：重启后 1 分钟内验证核心链路、边界与运行日志。

覆盖：健康/配置/显存规划、简单直答、中档异步、进度、3 轮召回、5K 长输入、
错误路径、5 路并发、上下文预算、运行日志异常。
"""
import json
import os
import pathlib
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = os.getenv("BASE", f"http://127.0.0.1:{os.getenv('API_PORT', '8100')}")
ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG_DIR = pathlib.Path(os.getenv("LOG_DIR", str(ROOT / "logs")))
RESULTS = []


def check(name, ok, detail, severity="hard"):
    RESULTS.append({"case": name, "ok": bool(ok), "severity": severity,
                    "detail": str(detail)[:500]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:220]}", flush=True)


def chat(text, sid, prefer=None, timeout=300):
    body = {"user_input": text, "session_id": sid}
    if prefer:
        body["prefer_tier"] = prefer
    return httpx.post(BASE + "/chat", json=body, timeout=timeout)


def wait_task(task_id, timeout=300):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = httpx.get(BASE + "/task_result", params={"task_id": task_id},
                         timeout=20).json()
        if last.get("task_status") in ("completed", "failed"):
            return last
        time.sleep(1.5)
    return last or {"task_status": "timeout"}


def filler(chars):
    unit = "本地大模型部署需要关注显存、量化、上下文、并发、KV cache 与回滚方案。"
    return (unit * (chars // len(unit) + 1))[:chars]


def main():
    sid = f"quick-{int(time.time())}"
    conf = httpx.get(BASE + "/config", timeout=30).json()
    ctx = conf["num_ctx"]
    plan = conf.get("vram_plan", {})

    check("health", httpx.get(BASE + "/health", timeout=30).json().get("status") == "success",
          f"ctx={ctx} backend={conf['backend']}")
    check("vram_plan_present",
          all(k in plan for k in conf["slots"])
          and all("available" in v for v in plan.values()),
          {k: v.get("available") for k, v in plan.items()})
    check("front_available", plan.get(conf["front_slot"], {}).get("available") is True,
          plan.get(conf["front_slot"]))

    r1 = chat("只回答一个数字：3+4 等于几？", sid).json()
    check("simple_direct", r1.get("process_mode") == "direct" and "7" in str(r1.get("instant_reply")),
          f"mode={r1.get('process_mode')} reply={r1.get('instant_reply')}")
    check("context_usage",
          r1.get("context_usage", {}).get("estimated_total_chars", ctx + 1) <= ctx,
          r1.get("context_usage"))

    r2 = chat("请用一句话说明什么是本地大模型路由", sid, "mid").json()
    d2 = wait_task(r2["task_id"])
    check("mid_async_completed",
          d2.get("task_status") == "completed" and bool(d2.get("final_reply")),
          f"slot={d2.get('chosen_model')} len={len(str(d2.get('final_reply') or ''))}")

    r3 = chat("进度怎么样了？", sid).json()
    check("progress_note", "[后台任务进度]" in str(r3.get("instant_reply", "")),
          str(r3.get("instant_reply"))[-120:])

    # 3 轮短多轮，随机标记，第 3 轮应能召回（只校验随机串本体，避免模型丢前缀误判）
    marks = {}
    for i in range(1, 4):
        marks[i] = secrets.token_hex(4).upper()
        chat(f"第{i}轮资料（标记 {marks[i]}）：{filler(400)}\n只回复收到", sid)
    rr = chat("第 3 轮资料里的标记是什么？只回答标记。", sid).json()
    check("multi_turn_recall", marks[3] in str(rr.get("instant_reply", "")),
          f"expect={marks[3]} reply={str(rr.get('instant_reply'))[:120]}")

    # 5K 长输入：允许且不越界；后台完成
    rl = chat(f"资料：{filler(5000)}\n请用一句话总结主题。", sid + "-long", "mid").json()
    dl = wait_task(rl["task_id"])
    check("long_input_5k",
          dl.get("task_status") == "completed"
          and len(str(dl.get("final_reply") or "")) >= 5,
          f"status={dl.get('task_status')} slot={dl.get('chosen_model')} "
          f"estimated={rl.get('context_usage', {}).get('estimated_total_chars')}")
    check("long_input_budget",
          rl.get("context_usage", {}).get("estimated_total_chars", ctx + 1) <= ctx,
          rl.get("context_usage"))

    # 错误路径
    e1 = httpx.post(BASE + "/chat", json={"user_input": " "}, timeout=30)
    e2 = httpx.post(BASE + "/chat", json={"user_input": "啊" * (conf["limits"]["max_input_chars"] + 1)},
                    timeout=30)
    e3 = httpx.post(BASE + "/chat", json={"user_input": "你好", "prefer_tier": "bad"}, timeout=30)
    check("errors", (e1.status_code, e2.status_code, e3.status_code) == (400, 413, 400),
          f"{e1.status_code}/{e2.status_code}/{e3.status_code}")

    # 5 路并发简单请求
    def one(i):
        try:
            r = httpx.post(BASE + "/chat", json={
                "user_input": f"并发{i}：只回复 OK", "session_id": sid + "-c"}, timeout=120)
            d = r.json()
            return r.status_code, bool(d.get("instant_reply"))
        except Exception as exc:
            return 0, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=5) as ex:
        conc = list(ex.map(one, range(5)))
    check("concurrent_5", all(c == 200 and ok for c, ok in conc), conc)

    h = httpx.get(BASE + "/health", timeout=30).json()
    check("no_backlog", h.get("queue_depth") == 0, h.get("queue_depth"))
    check("ctx_stable", h.get("num_ctx") == ctx, h.get("num_ctx"))

    # 运行日志：只看最近 300 行，避免历史记录误报
    bad = {}
    for name in ("api.log", "front.log", "mid.log", "moe.log"):
        p = LOG_DIR / name
        if not p.exists():
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
        hits = [ln for ln in lines if any(k in ln for k in
                ("500 Internal Server Error", "out of memory",
                 "roles must alternate", "Traceback"))]
        if hits:
            bad[name] = hits[-2:]
    check("runtime_logs_clean", not bad, bad or "最近 300 行无 500/OOM/角色交替异常")

    report = {"base": BASE, "num_ctx": ctx, "cases": RESULTS,
              "passed": sum(1 for x in RESULTS if x["ok"]),
              "total": len(RESULTS),
              "all_pass": all(x["ok"] for x in RESULTS)}
    out = LOG_DIR / "quick_check.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== QUICK {report['passed']}/{report['total']} all_pass={report['all_pass']} -> {out} ===")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

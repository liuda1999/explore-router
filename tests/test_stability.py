"""连续请求稳定性：句句有着落、无状态悬挂、上下文不漂移。

上下文期望值从 /health 读取，不写死具体档位，适配任意设备配置。
"""
import json
import os
import sys
import time

import httpx

BASE = os.getenv("BASE", "http://127.0.0.1:8100")
N = int(os.getenv("STRESS_N", "10"))


def main():
    sid = "stress-" + str(int(time.time()))
    h0 = httpx.get(BASE + "/health", timeout=30).json()
    expected_ctx = h0["num_ctx"]

    ok, bad = 0, []
    latencies = []
    for i in range(N):
        t0 = time.time()
        r = httpx.post(BASE + "/chat", json={
            "user_input": f"第 {i + 1} 条：简单确认收到即可",
            "session_id": sid,
        }, timeout=90)
        latencies.append(round((time.time() - t0) * 1000, 1))
        d = r.json()
        if r.status_code == 200 and d.get("process_mode") == "direct" and d.get("instant_reply"):
            ok += 1
        else:
            bad.append({"i": i, "status": r.status_code, "resp": str(d)[:200]})

    tasks = httpx.get(BASE + "/tasks?limit=100", timeout=30).json()["tasks"]
    mine = [t for t in tasks if t["session_id"] == sid]
    hanging = [t["task_id"] for t in mine if t["status"] not in ("completed", "failed")]
    h = httpx.get(BASE + "/health", timeout=30).json()

    report = {
        "expected_num_ctx": expected_ctx,
        "requests": N,
        "instant_ok": ok,
        "bad": bad,
        "session_tasks": len(mine),
        "hanging": hanging,
        "avg_ms": round(sum(latencies) / len(latencies), 1),
        "max_ms": max(latencies),
        "queue_depth": h["queue_depth"],
        "num_ctx": h["num_ctx"],
        "slot": h["slot"],
        "vram_free_mb": h["vram"]["free_mb"],
    }
    passed = (ok == N and not hanging and h["queue_depth"] == 0
              and h["num_ctx"] == expected_ctx)
    report["all_pass"] = passed
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

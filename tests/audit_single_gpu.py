"""单卡/小显存场景审计：重档装不下时应自动降级，而不是 OOM。

运行方式（在双卡机上限制为单卡，真实可见显存约 20G）：
    CUDA_VISIBLE_DEVICES=0 DATA_DIR=... ./run.sh start --wait
    python3 tests/audit_single_gpu.py
"""
import json
import os
import pathlib
import time

import httpx

BASE = os.getenv("BASE", f"http://127.0.0.1:{os.getenv('API_PORT', '8100')}")
ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS = []


def check(name, ok, detail, severity="hard"):
    RESULTS.append({"case": name, "ok": bool(ok), "severity": severity,
                    "detail": str(detail)[:600]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:240]}", flush=True)


def wait_task(task_id, timeout=600):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = httpx.get(BASE + "/task_result", params={"task_id": task_id},
                         timeout=20).json()
        if last.get("task_status") in ("completed", "failed"):
            return last
        time.sleep(2)
    return last or {"task_status": "timeout"}


def main():
    conf = httpx.get(BASE + "/config", timeout=30).json()
    plan = conf.get("vram_plan", {})
    t2s = conf["tier_to_slot"]
    mid_slot = t2s.get("mid") or conf["default_slot"]
    heavy_slot = t2s.get("heavy") or mid_slot

    check("single_gpu_detected", conf.get("gpu_count") == 1,
          f"gpu_count={conf.get('gpu_count')} total_vram={conf.get('total_vram_mb')}MB")
    check("front_available", plan.get(conf["front_slot"], {}).get("available") is True,
          plan.get(conf["front_slot"]))
    check("heavy_marked_unavailable",
          plan.get(heavy_slot, {}).get("available") is False,
          plan.get(heavy_slot))
    check("mid_available", plan.get(mid_slot, {}).get("available") is True,
          plan.get(mid_slot))

    h = httpx.get(BASE + "/health", timeout=30).json()
    check("frontend_ready", h.get("frontend") == "ready",
          f"frontend={h.get('frontend')} free={h.get('vram', {}).get('free_mb')}MB")

    sid = f"single-{int(time.time())}"
    r = httpx.post(BASE + "/chat", json={
        "user_input": "请详细分析本地推理的显存优化方案，并给出完整建议",
        "session_id": sid, "prefer_tier": "heavy",
    }, timeout=120)
    d = r.json() if r.status_code == 200 else {}
    check("heavy_request_accepted", r.status_code == 200,
          f"status={r.status_code} mode={d.get('process_mode')}")
    check("degraded_reason_visible",
          "降级" in str(d.get("selection_reason", "")),
          d.get("selection_reason"))

    if d.get("task_id"):
        done = wait_task(d["task_id"])
        check("degraded_task_completed",
              done.get("task_status") == "completed"
              and bool(done.get("final_reply")),
              f"status={done.get('task_status')} slot={done.get('chosen_model')} "
              f"err={done.get('error_msg')}")
        check("degraded_to_mid",
              done.get("chosen_model") == mid_slot,
              f"chosen={done.get('chosen_model')} expected={mid_slot}")
        check("release_mentions_degrade",
              "降级" in str(done.get("release_info", "")),
              done.get("release_info"))
    else:
        check("degraded_task_completed", False, "no task_id (可能被前台直接回答)")
        check("degraded_to_mid", False, "no task_id")
        check("release_mentions_degrade", False, "no task_id")

    h2 = httpx.get(BASE + "/health", timeout=30).json()
    check("no_oom_free_vram",
          h2.get("vram", {}).get("free_mb", 0) > 0
          and h2.get("frontend") == "ready",
          f"free={h2.get('vram', {}).get('free_mb')}MB front={h2.get('frontend')}")

    report = {"base": BASE, "config": conf, "cases": RESULTS,
              "passed": sum(1 for x in RESULTS if x["ok"]),
              "total": len(RESULTS),
              "all_pass": all(x["ok"] for x in RESULTS)}
    out = ROOT / "logs" / "audit_single_gpu.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== SINGLE GPU {report['passed']}/{report['total']} "
          f"all_pass={report['all_pass']} -> {out} ===")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

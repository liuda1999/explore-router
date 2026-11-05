"""重启恢复与队列上限审计：真实停服/启服，不使用 mock。

- 提交任务后立即重启：processing 应标记失败、pending 应重新入队，不能永久卡住；
- QUEUE_MAXSIZE=1 启动后连续提交：超限必须返回 429，而不是无限堆积。
"""
import json
import os
import pathlib
import subprocess
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = os.getenv("BASE", f"http://127.0.0.1:{os.getenv('API_PORT', '8100')}")
RESULTS = []


def check(name, ok, detail, severity="hard"):
    RESULTS.append({"case": name, "ok": bool(ok), "severity": severity,
                    "detail": str(detail)[:700]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:260]}", flush=True)


def base_env(extra=None):
    e = os.environ.copy()
    e.setdefault("DATA_DIR", str(ROOT / "data" / "audit"))
    e.setdefault("LOG_DIR", str(ROOT / "logs"))
    e.setdefault("PID_FILE", str(ROOT / "data" / "router.pid"))
    e.setdefault("LOG", str(ROOT / "logs" / "api.log"))
    e.setdefault("API_PORT", "8100")
    e.setdefault("FRONT_PORT", "18100")
    e.setdefault("SLOT_PORT", "18101")
    if extra:
        e.update({k: str(v) for k, v in extra.items()})
    return e


def run_sh(*args, extra=None):
    r = subprocess.run(["bash", str(ROOT / "run.sh"), *args],
                       env=base_env(extra), capture_output=True, text=True,
                       timeout=700)
    return {"code": r.returncode,
            "out": (r.stdout or "")[-1500:], "err": (r.stderr or "")[-1500:]}


def get_task(task_id):
    for _ in range(15):
        try:
            return httpx.get(BASE + "/task_result", params={"task_id": task_id},
                             timeout=15).json()
        except Exception:
            time.sleep(1)
    return {"task_status": "unreachable"}


def wait_task(task_id, timeout=420):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = get_task(task_id)
        if last.get("task_status") in ("completed", "failed"):
            return last
        time.sleep(2)
    return last or {"task_status": "timeout"}


def health():
    for _ in range(15):
        try:
            return httpx.get(BASE + "/health", timeout=15).json()
        except Exception:
            time.sleep(1)
    return {}


def main():
    sid = f"restart-{int(time.time())}"
    check("service_initial", health().get("status") == "success",
          health().get("frontend"))

    # ---------- 1. 提交后立刻重启 ----------
    r = httpx.post(BASE + "/chat", json={
        "user_input": "请详细分析本地推理的显存调度，并给出完整方案",
        "session_id": sid, "prefer_tier": "heavy",
    }, timeout=120)
    body = r.json()
    tid = body.get("task_id")
    check("task_submitted", r.status_code == 200 and bool(tid),
          f"status={r.status_code} task={tid} mode={body.get('process_mode')}")

    stop1 = run_sh("stop")
    start1 = run_sh("start", "--wait")
    check("restart_ok", stop1["code"] == 0 and start1["code"] == 0,
          f"stop={stop1['code']} start={start1['code']} out={start1['out'][-300:]}")

    d = wait_task(tid, timeout=420)
    check("task_not_stuck_after_restart",
          d.get("task_status") in ("completed", "failed"),
          f"status={d.get('task_status')} err={d.get('error_msg')} "
          f"slot={d.get('chosen_model')}")

    # 如果是 processing→failed，错误信息必须说明是重启导致
    if d.get("task_status") == "failed":
        check("restart_failure_explained",
              "重启" in str(d.get("error_msg", "")),
              str(d.get("error_msg"))[:200])

    # ---------- 2. 队列上限：QUEUE_MAXSIZE=1 ----------
    run_sh("stop")
    start_lim = run_sh("start", "--wait", extra={"QUEUE_MAXSIZE": "1"})
    check("limited_queue_start", start_lim["code"] == 0, start_lim["out"][-300:])

    codes = []
    tids = []
    for i in range(5):
        rr = httpx.post(BASE + "/chat", json={
            "user_input": f"请写一段关于缓存设计第{i + 1}部分的说明",
            "session_id": sid + "-q", "prefer_tier": "mid",
        }, timeout=120)
        codes.append(rr.status_code)
        if rr.status_code == 200:
            tids.append(rr.json().get("task_id"))

    check("queue_full_returns_429", 429 in codes,
          f"statuses={codes}（QUEUE_MAXSIZE=1，连续 5 次）")
    check("rejected_not_created_as_pending",
          len(tids) < 5, f"accepted={len(tids)}/5")

    for tid2 in tids:
        wait_task(tid2, timeout=300)

    # ---------- 3. 恢复默认配置 ----------
    run_sh("stop")
    restore = run_sh("start", "--wait")
    check("restore_default_config", restore["code"] == 0, restore["out"][-300:])
    h = health()
    check("restored_health", h.get("status") == "success"
          and h.get("frontend") == "ready",
          f"frontend={h.get('frontend')} ctx={h.get('num_ctx')}")

    report = {
        "base": BASE,
        "cases": RESULTS,
        "passed": sum(1 for x in RESULTS if x["ok"]),
        "total": len(RESULTS),
        "all_pass": all(x["ok"] for x in RESULTS),
        "finished_at": time.time(),
    }
    out = ROOT / "logs" / "audit_restart.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== RESTART {report['passed']}/{report['total']} "
          f"all_pass={report['all_pass']} -> {out} ===")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

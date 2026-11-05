"""真实故障注入：前台/后台模型进程被杀后的自愈，以及启动防护。

不使用 mock：直接对真实 llama-server 进程发 SIGKILL，观察服务是否恢复。
"""
import asyncio
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import httpx

BASE = os.getenv("BASE", f"http://127.0.0.1:{os.getenv('API_PORT', '8100')}")
ROOT = pathlib.Path(__file__).resolve().parent.parent
SLOT_PORT = int(os.getenv("SLOT_PORT", "18101"))
FRONT_PORT = int(os.getenv("FRONT_PORT", "18100"))
RESULTS = []


def check(name, ok, detail, severity="hard"):
    RESULTS.append({"case": name, "ok": bool(ok), "severity": severity,
                    "detail": str(detail)[:600]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:240]}", flush=True)


def health():
    return httpx.get(BASE + "/health", timeout=30).json()


def cfg():
    return httpx.get(BASE + "/config", timeout=30).json()


def post_chat(text, sid, prefer=None, timeout=180):
    body = {"user_input": text, "session_id": sid}
    if prefer:
        body["prefer_tier"] = prefer
    return httpx.post(BASE + "/chat", json=body, timeout=timeout)


def wait_task(task_id, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = httpx.get(BASE + "/task_result", params={"task_id": task_id},
                      timeout=20).json()
        if d.get("task_status") in ("completed", "failed"):
            return d
        time.sleep(2)
    return {"task_status": "timeout"}


def server_pid(port):
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"llama-server.*--port {port}"],
            capture_output=True, text=True, timeout=10)
        pids = [x for x in out.stdout.split() if x.isdigit()]
        return int(pids[0]) if pids else None
    except Exception:
        return None


def wait_front_state(state, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if health().get("frontend") == state:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main():
    sid = f"fault-{int(time.time())}"
    conf = cfg()
    h0 = health()
    check("initial_ready", h0.get("frontend") == "ready", h0.get("frontend"))

    # ---------- 1. 前台进程被 SIGKILL 后自愈 ----------
    fpid = server_pid(FRONT_PORT)
    check("front_pid_found", fpid is not None, f"pid={fpid} port={FRONT_PORT}")
    if fpid:
        os.kill(fpid, signal.SIGKILL)
        down = wait_front_state("down", 25)
        check("front_detected_down", down, "health.frontend == down")
        t0 = time.time()
        r = post_chat("前台自愈测试：只回复 OK", sid)
        d = r.json() if r.status_code == 200 else {}
        elapsed = round(time.time() - t0, 1)
        check("front_auto_restart",
              r.status_code == 200 and bool(d.get("instant_reply"))
              and "前台模型暂时不可用" not in str(d.get("instant_reply")),
              f"status={r.status_code} {elapsed}s reply={str(d.get('instant_reply'))[:100]}")
        check("front_ready_again", wait_front_state("ready", 30),
              health().get("frontend"))

    # ---------- 2. 后台进程被 SIGKILL 后按需重启 ----------
    h = health()
    cur = h.get("slot", {}).get("current")
    tier = next((t for t, s in conf["tier_to_slot"].items() if s == cur), None)
    if not cur or not tier:
        # 没有后台模型驻留时先加载一个中档模型
        tier = conf["tier_order"][1]
        r0 = post_chat("加载测试：写一句关于缓存的说明", sid, tier)
        wait_task(r0.json()["task_id"])
        h = health()
        cur = h["slot"]["current"]
        tier = next((t for t, s in conf["tier_to_slot"].items() if s == cur), tier)
    spid = server_pid(SLOT_PORT)
    check("slot_pid_found", spid is not None, f"pid={spid} slot={cur} tier={tier}")
    if spid:
        os.kill(spid, signal.SIGKILL)
        time.sleep(2)
        r = post_chat("后台自愈测试：写一句关于队列的说明", sid, tier)
        tid = r.json().get("task_id") if r.status_code == 200 else None
        d = wait_task(tid) if tid else {"task_status": "no-task"}
        check("slot_auto_restart",
              d.get("task_status") == "completed" and bool(d.get("final_reply")),
              f"status={d.get('task_status')} slot={d.get('chosen_model')} "
              f"err={d.get('error_msg')}")
        check("slot_ready_again",
              health().get("slot", {}).get("current") in conf["slots"],
              str(health().get("slot")))

    # ---------- 3. 启动防护：模型文件缺失 / 端口占用 ----------
    sys.path.insert(0, str(ROOT))
    from app import config as appconfig
    from app.llama import LlamaServer

    missing = LlamaServer("probe-missing", 18199,
                          pathlib.Path("/nonexistent/model.gguf"), [])
    try:
        asyncio.run(missing.start())
        err_missing = ""
    except Exception as exc:
        err_missing = f"{type(exc).__name__}: {exc}"
    check("missing_model_fails_fast",
          "model file missing" in err_missing, err_missing)

    busy = LlamaServer("probe-busy", int(os.getenv("API_PORT", "8100")),
                       appconfig.MODELS_META[appconfig.FRONT_KEY]["path"], [])
    try:
        asyncio.run(busy.start())
        err_busy = ""
    except Exception as exc:
        err_busy = f"{type(exc).__name__}: {exc}"
    check("port_busy_fails_fast", "already in use" in err_busy, err_busy)

    # ---------- 汇总 ----------
    report = {
        "base": BASE,
        "cases": RESULTS,
        "passed": sum(1 for x in RESULTS if x["ok"]),
        "total": len(RESULTS),
        "all_pass": all(x["ok"] for x in RESULTS),
        "finished_at": time.time(),
    }
    out = ROOT / "logs" / "audit_faults.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== FAULTS {report['passed']}/{report['total']} all_pass={report['all_pass']} -> {out} ===")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""端到端冒烟测试：配置驱动，不写死后端、槽位名或模型。"""
import json
import os
import sys
import time

import httpx

BASE = os.getenv("BASE", "http://127.0.0.1:8100")
TIMEOUT = float(os.getenv("SMOKE_TIMEOUT", "420"))
results = []


def check(name, ok, detail):
    results.append({"case": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def wait_task(task_id, timeout=TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = httpx.get(BASE + "/task_result", params={"task_id": task_id}, timeout=20).json()
        if d.get("task_status") in ("completed", "failed"):
            return d
        time.sleep(3)
    return {"task_status": "timeout"}


def main():
    c = httpx.Client(base_url=BASE, timeout=30)
    h = c.get("/health").json()
    cfg = c.get("/config").json()
    backend_slots = [k for k, v in cfg["slots"].items() if not v["resident"]]

    check("health", h.get("status") == "success",
          f"backend={h.get('backend')} frontend={h.get('frontend')}")
    check("gpu_backend_available",
          h.get("backend") in ("cuda", "rocm") and cfg.get("gpu_count", 0) >= 1,
          f"backend={h.get('backend')} gpu_count={cfg.get('gpu_count')}")
    check("num_ctx_constant", isinstance(h.get("num_ctx"), int) and h["num_ctx"] > 0,
          f"num_ctx={h.get('num_ctx')} source={cfg.get('num_ctx_source')}")
    check("embedding", h.get("embedding", {}).get("enabled") is True,
          f"embed={h.get('embedding')}")

    sid = "smoke-" + str(int(time.time()))
    r1 = c.post("/chat", json={"user_input": "你好，简单自我介绍一句", "session_id": sid}).json()
    check("simple_direct", r1.get("process_mode") == "direct" and bool(r1.get("instant_reply")),
          f"mode={r1.get('process_mode')} reply={str(r1.get('instant_reply'))[:60]}")

    r2 = c.post("/chat", json={"user_input": "请详细分析本地大模型路由的设计要点，并给出一份实现方案",
                               "session_id": sid}).json()
    check("complex_async", r2.get("process_mode") == "async" and bool(r2.get("task_id")),
          f"mode={r2.get('process_mode')} tier={r2.get('task_type')} reason={r2.get('selection_reason')}")
    check("instant_reply_present", bool(r2.get("instant_reply")),
          f"instant={str(r2.get('instant_reply'))[:60]}")

    r3 = c.post("/chat", json={"user_input": "进度怎么样了？", "session_id": sid}).json()
    check("progress_answer", bool(r3.get("instant_reply")),
          f"reply={str(r3.get('instant_reply'))[:80]}")

    final = wait_task(r2["task_id"])
    check("final_reply", final.get("task_status") == "completed" and len(final.get("final_reply", "")) >= 5,
          f"status={final.get('task_status')} len={len(final.get('final_reply',''))} "
          f"slot={final.get('chosen_model')} ms={final.get('prefill_ms')} err={final.get('error_msg')}")

    h2 = c.get("/health").json()
    check("ctx_unchanged_after_task", h2.get("num_ctx") == h.get("num_ctx"),
          f"before={h.get('num_ctx')} after={h2.get('num_ctx')}")
    check("slot_resident", h2.get("slot", {}).get("current") in backend_slots,
          f"slot={h2.get('slot')} backend_slots={backend_slots}")

    all_ok = all(x["ok"] for x in results)
    print("\n=== SUMMARY ===")
    print(json.dumps({"all_pass": all_ok, "config": cfg, "cases": results},
                     ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

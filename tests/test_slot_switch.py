"""专项测试：后台槽位互斥切换、同档复用、上下文恒定、会话连续。

槽位名与档位名全部从 /config 读取，换模型或改槽位配置后无需改测试。
"""
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


def health():
    return httpx.get(BASE + "/health", timeout=30).json()


def chat(text, sid, prefer=None):
    body = {"user_input": text, "session_id": sid}
    if prefer:
        body["prefer_tier"] = prefer
    return httpx.post(BASE + "/chat", json=body, timeout=60).json()


def wait(task_id):
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        d = httpx.get(BASE + "/task_result", params={"task_id": task_id}, timeout=20).json()
        if d.get("task_status") in ("completed", "failed"):
            return d
        time.sleep(3)
    return {"task_status": "timeout"}


def main():
    cfg = httpx.get(BASE + "/config", timeout=30).json()
    t2s = cfg["tier_to_slot"]
    plan = cfg.get("vram_plan", {})
    backend_slots = [k for k, v in cfg["slots"].items() if not v["resident"]]
    # 单卡/小显存时某些槽位会被判不可用，期望值要按可用集合动态调整
    avail_backend = [k for k in backend_slots
                     if plan.get(k, {}).get("available", True)]
    mid_slot = t2s.get("mid") or (avail_backend[0] if avail_backend
                                  else (backend_slots[0] if backend_slots else cfg["front_slot"]))
    heavy_slot = t2s.get("heavy") or (backend_slots[-1] if backend_slots else mid_slot)
    heavy_available = bool(plan.get(heavy_slot, {}).get("available", True))
    mid_tier = next((t for t, s in t2s.items() if s == mid_slot), "mid")
    heavy_tier = next((t for t, s in t2s.items() if s == heavy_slot), "heavy")
    multi = len(set(avail_backend)) >= 2

    sid = "slot-" + str(int(time.time()))
    h0 = health()
    s0 = h0["slot"]["switches"]
    check("initial_health", h0["status"] == "success",
          f"backend={h0['backend']} ctx={h0['num_ctx']} slot={h0['slot']['current']}")

    r1 = chat("请写一段关于本地模型部署的简短说明", sid, mid_tier)
    d1 = wait(r1["task_id"])
    h1 = health()
    check("mid_task", d1.get("task_status") == "completed" and len(d1.get("final_reply", "")) >= 5,
          f"slot={d1.get('chosen_model')} len={len(d1.get('final_reply',''))} ms={d1.get('prefill_ms')}")
    check("switch_to_mid", h1["slot"]["current"] in (avail_backend + [cfg["front_slot"]]),
          f"slot={h1['slot']['current']} available={avail_backend}")

    r2 = chat("再补充两点部署时的注意事项", sid, mid_tier)
    d2 = wait(r2["task_id"])
    h2 = health()
    check("same_tier_no_switch", h2["slot"]["switches"] == h1["slot"]["switches"],
          f"switches {h1['slot']['switches']} -> {h2['slot']['switches']} (should not change)")
    check("second_mid_result", len(d2.get("final_reply", "")) >= 5,
          f"len={len(d2.get('final_reply',''))} err={d2.get('error_msg')}")

    r3 = chat("请详细分析本地智能路由的整体架构并给出完整实现方案", sid, heavy_tier)
    d3 = wait(r3["task_id"])
    h3 = health()
    check("heavy_task", d3.get("task_status") == "completed" and len(d3.get("final_reply", "")) >= 5,
          f"slot={d3.get('chosen_model')} len={len(d3.get('final_reply',''))} ms={d3.get('prefill_ms')} "
          f"reason={str(d3.get('selection_reason'))[:80]}")
    if heavy_available:
        check("switch_to_heavy", h3["slot"]["current"] == heavy_slot,
              f"slot={h3['slot']['current']} heavy={heavy_slot}")
        check("switch_counted", h3["slot"]["switches"] >= s0 + 1,
              f"total switches={h3['slot']['switches']}")
    else:
        check("heavy_degraded_no_switch",
              d3.get("chosen_model") in (avail_backend + [cfg["front_slot"]])
              and h3["slot"]["switches"] == h2["slot"]["switches"],
              f"{heavy_slot} 不可用 -> chosen={d3.get('chosen_model')} "
              f"switches={h2['slot']['switches']}->{h3['slot']['switches']}（不应再切换）")

    ctxs = [h0["num_ctx"], h1["num_ctx"], h2["num_ctx"], h3["num_ctx"],
            d1.get("num_ctx"), d2.get("num_ctx"), d3.get("num_ctx")]
    check("ctx_constant_everywhere", len(set(ctxs)) == 1,
          f"all num_ctx={ctxs}")
    check("sessions_persist", all(x.get("instant_reply") for x in (r1, r2, r3)),
          "instant_reply present on every turn")
    check("embedding_vector_used", h3["embedding"].get("dim", 0) > 0,
          f"dim={h3['embedding'].get('dim')}")
    check("vram_positive", h3["vram"]["free_mb"] > 0,
          f"free={h3['vram']['free_mb']}MB used={h3['vram']['used_mb']}MB")

    ok = all(x["ok"] for x in results)
    print("\n=== SUMMARY ===")
    print(json.dumps({"all_pass": ok, "slots": {"mid": mid_slot, "heavy": heavy_slot},
                      "multi_slot": multi, "heavy_available": heavy_available,
                      "cases": results}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

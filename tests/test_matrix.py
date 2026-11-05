"""单档上下文完整测试：功能链路 + 槽位切换 + 生成速度 + 上下文恒定。

端口、槽位名、档位名全部从 /config 读取；换设备或改 settings.yaml 无需改测试。
用法: python3 tests/test_matrix.py --ctx <该设备可用的 N>
结果写入 logs/matrix_<ctx>.json
"""
import argparse
import json
import os
import pathlib
import subprocess
import time

import httpx

BASE = os.getenv("BASE", f"http://127.0.0.1:{os.getenv('API_PORT', '8100')}")
ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG_DIR = pathlib.Path(os.getenv("LOG_DIR", str(ROOT / "logs")))
CTX = None


def cfg():
    return httpx.get(BASE + "/config", timeout=30).json()


def health():
    return httpx.get(BASE + "/health", timeout=30).json()


def chat(text, sid, prefer=None):
    body = {"user_input": text, "session_id": sid}
    if prefer:
        body["prefer_tier"] = prefer
    return httpx.post(BASE + "/chat", json=body, timeout=90).json()


def wait(task_id, timeout=420):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = httpx.get(BASE + "/task_result", params={"task_id": task_id}, timeout=20).json()
        if d.get("task_status") in ("completed", "failed"):
            return d
        time.sleep(3)
    return {"task_status": "timeout"}


def bench(port, label, max_tokens=320):
    """直接调运行时拿 timings，测真实 token 生成速度。"""
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    payload = {
        "messages": [{"role": "user", "content":
                      "请用约 250 字说明本地大模型智能路由的工作原理、模型分级依据和显存调度策略。"}],
        "max_tokens": max_tokens, "temperature": 0.3, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        r = httpx.post(url, json=payload, timeout=240)
        if r.status_code != 200:
            payload.pop("chat_template_kwargs", None)
            r = httpx.post(url, json=payload, timeout=240)
        d = r.json()
        t = d.get("timings", {})
        msg = (d.get("choices") or [{}])[0].get("message", {})
        return {
            "label": label, "ok": True,
            "predicted_tokens": t.get("predicted_n"),
            "predicted_per_second": t.get("predicted_per_second"),
            "prompt_per_second": t.get("prompt_per_second"),
            "predicted_ms": t.get("predicted_ms"),
            "content_len": len(msg.get("content") or ""),
            "finish_reason": (d.get("choices") or [{}])[0].get("finish_reason"),
            "http_status": r.status_code,
        }
    except Exception as exc:
        return {"label": label, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def ctx_slots_in_logs():
    """扫描所有运行时日志，不假定槽位名；取每个文件最后一次启动的 n_ctx_slot。"""
    out = []
    for p in sorted(LOG_DIR.glob("*.log")):
        if p.name == "api.log":
            continue
        try:
            lines = subprocess.run(["grep", "-h", "n_ctx_slot", str(p)],
                                   capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
            vals = [int(x.split("n_ctx_slot = ")[1].split(",")[0])
                    for x in lines if "n_ctx_slot = " in x]
            if vals:
                out.append({"file": p.name, "last": vals[-1], "values": vals[-3:]})
        except Exception as exc:
            out.append({"file": p.name, "error": str(exc)})
    return out


def main():
    global CTX
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctx", type=int, required=True)
    CTX = ap.parse_args().ctx

    c = cfg()
    ports = c["ports"]
    t2s = c["tier_to_slot"]
    backend_slots = [k for k, v in c["slots"].items() if not v["resident"]]
    mid_slot = t2s.get("mid") or (backend_slots[0] if backend_slots else c["front_slot"])
    heavy_slot = t2s.get("heavy") or (backend_slots[-1] if backend_slots else mid_slot)
    mid_tier = next((t for t, s in t2s.items() if s == mid_slot), "mid")
    heavy_tier = next((t for t, s in t2s.items() if s == heavy_slot), "heavy")

    report = {"ctx": CTX, "config": c, "cases": [], "speeds": [], "vram": [],
              "started_at": time.time()}
    results = []

    def check(name, ok, detail):
        results.append({"case": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] ctx={CTX} {name}: {detail}", flush=True)

    h0 = health()
    report["vram"].append({"stage": "initial", **h0["vram"]})
    check("gpu_backend_available", h0["backend"] in ("cuda", "rocm"), h0["backend"])
    check("ctx_matches", h0["num_ctx"] == CTX, f"expected={CTX} actual={h0['num_ctx']}")
    check("frontend_ready", h0["frontend"] == "ready", h0["frontend"])
    check("embedding", h0["embedding"]["enabled"], str(h0["embedding"])[:160])

    sid = f"matrix-{CTX}-{int(time.time())}"
    speed_front = bench(ports["front"], c["front_slot"])
    report["speeds"].append(speed_front)
    check("speed_front_measured", speed_front.get("ok") is True,
          f"{speed_front.get('predicted_per_second')} tok/s, n={speed_front.get('predicted_tokens')}")

    r1 = chat("你好，一句话确认收到", sid)
    check("simple_direct", r1.get("process_mode") == "direct" and bool(r1.get("instant_reply")),
          str(r1.get("instant_reply"))[:60])

    r2 = chat("请写一段关于本地模型部署的注意事项", sid, mid_tier)
    d2 = wait(r2["task_id"])
    h2 = health()
    report["vram"].append({"stage": "mid_loaded", **h2["vram"]})
    check("mid_task", d2.get("task_status") == "completed" and len(d2.get("final_reply", "")) >= 5,
          f"slot={d2.get('chosen_model')} len={len(d2.get('final_reply',''))} err={d2.get('error_msg')}")
    check("ctx_in_mid_task", d2.get("num_ctx") == CTX, d2.get("num_ctx"))
    check("slot_mid", h2["slot"]["current"] in backend_slots, str(h2["slot"]))

    speed_mid = bench(ports["slot"], mid_slot)
    report["speeds"].append(speed_mid)
    check("speed_mid_measured", speed_mid.get("ok") is True,
          f"{speed_mid.get('predicted_per_second')} tok/s, n={speed_mid.get('predicted_tokens')}")

    r3 = chat("进度怎么样了？", sid)
    pr = r3.get("instant_reply") or ""
    check("progress_answer_with_real_status",
          bool(pr) and "[后台任务进度]" in pr and "%" in pr, str(pr)[:110])

    r4 = chat("再加一条部署建议", sid, mid_tier)
    d4 = wait(r4["task_id"])
    h4 = health()
    check("same_tier_no_switch", h4["slot"]["switches"] == h2["slot"]["switches"],
          f"{h2['slot']['switches']} -> {h4['slot']['switches']}")
    check("mid_second_result", len(d4.get("final_reply", "")) >= 5,
          f"len={len(d4.get('final_reply',''))}")

    r5 = chat("请详细分析本地智能路由的整体架构并给出完整实现方案", sid, heavy_tier)
    d5 = wait(r5["task_id"])
    h5 = health()
    report["vram"].append({"stage": "heavy_loaded", **h5["vram"]})
    check("heavy_task", d5.get("task_status") == "completed" and len(d5.get("final_reply", "")) >= 5,
          f"slot={d5.get('chosen_model')} len={len(d5.get('final_reply',''))} err={d5.get('error_msg')}")
    check("ctx_in_heavy_task", d5.get("num_ctx") == CTX, d5.get("num_ctx"))
    check("slot_heavy", h5["slot"]["current"] in backend_slots, str(h5["slot"]))
    check("result_is_backend_output",
          bool(d5.get("final_reply")) and d5.get("final_reply") != r5.get("instant_reply"),
          "final_reply differs from instant_reply (no rewrite by frontend)")

    speed_heavy = bench(ports["slot"], heavy_slot)
    report["speeds"].append(speed_heavy)
    check("speed_heavy_measured", speed_heavy.get("ok") is True,
          f"{speed_heavy.get('predicted_per_second')} tok/s, n={speed_heavy.get('predicted_tokens')}")

    h6 = health()
    check("ctx_stable_after_all", h6["num_ctx"] == CTX, h6["num_ctx"])
    check("no_hanging_queue", h6["queue_depth"] == 0, h6["queue_depth"])
    check("embedding_vector", h6["embedding"].get("dim", 0) > 0, h6["embedding"].get("dim"))
    check("vram_positive", h6["vram"]["free_mb"] > 0, str(h6["vram"]))

    logs = ctx_slots_in_logs()
    report["log_ctx_slots"] = logs
    all_vals = [f["last"] for f in logs if f.get("last") is not None]
    check("logs_ctx_consistent", bool(all_vals) and all(v == CTX for v in all_vals),
          f"last per instance={all_vals}")

    report["cases"] = results
    report["slot_final"] = h6["slot"]
    report["finished_at"] = time.time()
    report["all_pass"] = all(x["ok"] for x in results)
    report["duration_s"] = round(report["finished_at"] - report["started_at"], 1)

    out = LOG_DIR / f"matrix_{CTX}.json"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"=== ctx={CTX} all_pass={report['all_pass']} duration={report['duration_s']}s -> {out} ===")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

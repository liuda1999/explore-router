"""真机多轮消息传递测试：三轮不同数据量，全部走真实 HTTP 与真实模型。

用法（在已启动服务的工作区根目录）：
    python3 tests/msg_rounds_test.py 1|2|3
结果：logs/msgtest_round<N>.json，stdout 打印摘要。
"""
import json
import random
import string
import sys
import time
import urllib.error
import os
import urllib.request

BASE = "http://127.0.0.1:8100"
RNG = random.SystemRandom()


def http_json(path, payload=None, timeout=240):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(
        BASE + path, data=data, headers=headers,
        method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, {"error": body[:400]}
    except Exception as exc:
        return -1, {"error": f"{type(exc).__name__}: {exc}"}


def wait_task(task_id, timeout=300):
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        _, d = http_json(f"/task_result?task_id={task_id}", timeout=30)
        last = d
        if d.get("task_status") in ("completed", "failed"):
            return d
        time.sleep(1.5)
    return last


def turn(session_id, text, tier=None):
    payload = {"user_input": text, "session_id": session_id}
    if tier:
        payload["prefer_tier"] = tier
    t0 = time.time()
    status, d = http_json("/chat", payload)
    row = {
        "chars": len(text),
        "http": status,
        "mode": d.get("process_mode"),
        "tier": d.get("task_type"),
        "instant": d.get("instant_reply") or "",
        "context": d.get("context_usage") or {},
        "task_id": d.get("task_id"),
        "chat_s": round(time.time() - t0, 2),
    }
    if status == 200 and d.get("process_mode") == "async":
        tr = wait_task(d["task_id"])
        row.update({
            "task_status": tr.get("task_status"),
            "final": tr.get("final_reply") or "",
            "chosen": tr.get("chosen_model"),
            "prefill_ms": tr.get("prefill_ms"),
            "release": tr.get("release_info") or "",
        })
    else:
        row.update({
            "task_status": "direct" if status == 200 else "http_error",
            "final": row["instant"] if status == 200 else d.get("error", ""),
            "chosen": "front",
            "prefill_ms": None,
            "release": "",
        })
    row["total_s"] = round(time.time() - t0, 2)
    return row


def marker(prefix):
    tail = "".join(RNG.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"{prefix}-{tail}"


def has_marker(reply, mark):
    norm = lambda s: "".join(str(s).split()).upper()
    return norm(mark) in norm(reply)


def filler(n, tag):
    base = ("本地推理的核心约束是显存与上下文的平衡。前台模型常驻负责即时响应，"
            "后台模型按需加载负责复杂任务；路由器依据设备实际资源选择槽位，"
            "并在结果返回后原样递回，不做二次改写。")
    parts = [f"【材料标记 {tag}】"]
    i = 0
    while sum(len(p) for p in parts) < n:
        i += 1
        parts.append(f"第{i}段：{base}这一段用于构造约{n}字的真实输入，第{i}段结束。")
    return "\n\n".join(parts)[:n]


def summarize(rows):
    print(f"{'轮':>2} {'字符':>6} {'HTTP':>4} {'模式':<8} {'档位':<7} "
          f"{'即时':>4} {'最终':>5} {'槽位':<6} {'任务状态':<10} {'耗时s':>6}")
    for i, r in enumerate(rows, 1):
        print(f"{i:>2} {r['chars']:>6} {r['http']:>4} {str(r['mode']):<8} "
              f"{str(r['tier']):<7} {len(r['instant']):>4} {len(r['final']):>5} "
              f"{str(r['chosen']):<6} {str(r['task_status']):<10} {r['total_s']:>6}")
        c = r["context"]
        if c:
            print(f"    ctx: input={c.get('input_chars')} history={c.get('history_chars')}"
                  f"/{c.get('history_messages')}条 budget={c.get('history_budget_chars')}"
                  f" estimated={c.get('estimated_total_chars')} num_ctx={c.get('num_ctx')}"
                  f" truncated={c.get('history_truncated')}")


def run_round(n):
    os.makedirs("logs", exist_ok=True)
    if n == 1:
        name = "小数据量（短对话，全部前台直答）"
        sid = "msg-r1-" + str(int(time.time()))
        m1 = marker("R1")
        script = [
            (f"你好，请记住暗号 {m1}，只回复“收到”。", "simple"),
            ("1+1 等于几？", "simple"),
            ("用一句话说明什么是本地智能路由。", "simple"),
            ("我刚才让你记住的暗号是什么？只回答暗号。", "simple"),
            ("再重复一次那个暗号。", "simple"),
        ]
    elif n == 2:
        name = "中等数据量（300-900 字/轮，前台+中档+重档混合）"
        sid = "msg-r2-" + str(int(time.time()))
        m1, m2, m3 = marker("R2A"), marker("R2B"), marker("R2C")
        script = [
            (filler(300, m1) + "\n\n请用一句话确认你收到了材料标记。", "simple"),
            (filler(350, m2) + "\n\n请分析这段需求，给出三点建议。", "mid"),
            (filler(600, m3) + "\n\n请详细分析并给出完整方案。", "heavy"),
            ("请复述前面三次材料里出现的所有标记，用逗号分隔，只输出标记。", "simple"),
            (filler(400, "R2D") + "\n\n请用不超过 100 字总结要点。", "simple"),
        ]
    else:
        name = "大数据量（3000-5000 字/轮，压上下文预算）"
        sid = "msg-r3-" + str(int(time.time()))
        m1, m2 = marker("R3A"), marker("R3B")
        script = [
            (filler(3500, m1) + "\n\n请用一句话确认你收到了材料标记。", "simple"),
            (filler(4000, m2) + "\n\n请提取三个关键点。", "heavy"),
            (filler(4500, "R3C") + "\n\n请基于以上内容给出完整方案。", "heavy"),
            ("第一轮材料里出现的标记是什么？只回答标记。", "simple"),
            ("用 200 字以内总结全部内容。", "simple"),
        ]

    print(f"\n=== ROUND {n}: {name} ===")
    print("session:", sid)
    rows = [turn(sid, text, tier) for text, tier in script]
    summarize(rows)

    result = {"round": n, "name": name, "session_id": sid, "rows": rows,
              "markers": [m for m in (locals().get("m1"), locals().get("m2"),
                                      locals().get("m3")) if m],
              "checks": {}}
    if n == 1:
        # 只看目标轮次（第 4、5 轮），不看历史里是否出现过
        replies = rows[3]["instant"] + " " + rows[3]["final"] + " " + rows[4]["instant"]
        result["checks"]["recall_marker_at_turn4_5"] = has_marker(replies, m1)
        result["checks"]["all_direct"] = all(r["mode"] == "direct" for r in rows)
        result["checks"]["all_http_200"] = all(r["http"] == 200 for r in rows)
    elif n == 2:
        t4 = rows[3]["instant"] + " " + rows[3]["final"]
        result["checks"]["recall_all_at_turn4"] = all(
            has_marker(t4, m) for m in (m1, m2, m3))
        result["checks"]["all_http_200"] = all(r["http"] == 200 for r in rows)
        result["checks"]["backend_completed"] = all(
            r["task_status"] == "completed" for r in rows if r["mode"] == "async")
    else:
        t4 = rows[3]["instant"] + " " + rows[3]["final"]
        result["checks"]["turn4_recall_R3A"] = has_marker(t4, m1)
        result["checks"]["turn4_recall_R3B"] = has_marker(t4, m2)
        result["checks"]["turn4_admits_unknown"] = any(
            k in t4 for k in ("无法回忆", "没有记录", "未记录", "无法确定",
                              "无法获取", "不确定", "没有相关", "不可用"))
        result["checks"]["truncated_flag_seen"] = any(
            r["context"].get("history_truncated") for r in rows)
        result["checks"]["all_http_200"] = all(r["http"] == 200 for r in rows)
        result["checks"]["no_overflow"] = all(
            r["context"].get("estimated_total_chars", 0) <= r["context"].get("num_ctx", 0)
            for r in rows if r["context"])
        result["checks"]["backend_completed"] = all(
            r["task_status"] == "completed" for r in rows if r["mode"] == "async")

    print("checks:", json.dumps(result["checks"], ensure_ascii=False))
    path = f"logs/msgtest_round{n}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("saved:", path)
    return result


if __name__ == "__main__":
    run_round(int(sys.argv[1]))

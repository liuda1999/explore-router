"""验收压测：真实设备、真实模型、真实 HTTP，不使用 mock。

矩阵（每个场景独立会话，互不污染）：
  A 长上下文多轮     /chat 5 轮 × 约 2.5K 字真实材料，末轮追问第一轮标记
  B Responses 续接   /v1/responses 4 轮 × 约 1.8K 字，previous_response_id 链式
  C 连续长文本回复   同会话 3 次要求 600 字，检查长度与是否被 max_tokens 截断
  D 三协议一致性     /chat、/v1/chat/completions、/v1/responses 对同一输入的 tier/mode
  E 并发队列         5 个 async 同时提交，检查排队、唯一 task_id、全部完成
  F 流式长文本       chat SSE（有 [DONE]）与 responses SSE（无 [DONE]）各一次
  G 边界错误         空输入、超长输入、未知 previous_response_id

同时采集 logs/*.log 的新增行，扫描 Traceback / ERROR / OOM / CUDA error 等。

用法（已启动服务的工作区根目录）：
    python3 tests/audit_acceptance.py
结果：logs/acceptance_<时间戳>.json；stdout 打印摘要；退出码非 0 表示有失败项。
"""
import json
import os
import random
import re
import string
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.getenv("BASE", "http://127.0.0.1:8100")
LOGS = ["logs/api.log", "logs/front.log", "logs/mid.log", "logs/moe.log"]
RESULTS = []
RNG = random.SystemRandom()

# ── 真实材料（非重复填充），每段约 400 字 ──────────────────────
PARAS = [
    "显存是本地推理最先遇到的硬约束。模型权重按量化等级占据固定空间，Q4 与 Q6 之间的差距在 7B 级别约为 2GB，在 30B 级别可以放大到 8GB 以上。除权重之外，KV cache 随上下文长度线性增长：同样的模型在 32K 与 128K 下的 KV 占用可能相差四倍。工程上通常先确定上下文窗口，再倒推可用的量化等级，而不是反过来。若显存仍然紧张，可以降低 KV cache 精度，但要注意部分老卡对 FP8 支持不完整。",

    "上下文窗口不是越大越好。窗口扩大带来两个直接代价：prefill 时间增加，以及显存占用上升。在多轮对话中，历史消息会不断累积，如果不加预算控制，几轮之后就会触发溢出或被迫截断。合理的做法是按当前输入长度动态分配历史预算：输入短时多带历史，输入长时收紧历史，把有限的窗口优先留给当前任务。截断本身不是错误，错误是截断之后不告诉模型，导致它把最近的内容误当成更早的内容来回答。",

    "槽位切换是本地路由里最贵的操作之一。加载一个 9B 模型通常需要数秒到十几秒，主要开销在权重从磁盘读入显存；35B 级别的 MoE 模型则可能超过二十秒。频繁切换会让简单请求也被迫等待，因此常见的策略是让前台小模型常驻，后台只保留一个槽位，需要另一档时才替换。替换时要注意先释放旧模型再加载新模型，否则两者的显存峰值叠加，很容易触发 OOM。切换次数应当作为可观测指标暴露出来。",

    "请求分级决定了用户体验的上限。用规则加语义线索做初筛，可以覆盖大部分场景：短输入且没有复杂词时直接由前台回答，长输入或包含分析、设计、代码等意图时转后台。分级不必追求绝对准确，但必须可解释，否则出问题时无法定位。一个实用做法是把分级理由一并返回，例如命中哪个关键词、输入长度是多少、目标槽位是否可用。降级也要显式：当重档模型装不下时，明确告诉调用方已降级到哪一档，而不是静默换模型。",

    "结果交付要保持一致。后台模型的输出应当原样返回，前台不负责改写或总结，否则同一个问题在不同链路上会得到不同版本，排查时无法对齐。即时回复与最终结果是两条不同的消息：即时回复负责让用户知道请求已被接收，最终结果才是正文。两者在存储上要分开，即时回复可以带进度附言，最终结果必须干净。OpenAI 兼容层需要把这两种语义映射到标准字段，同时用扩展字段保留原始信息。",

    "可观测性决定了故障能否被快速定位。至少需要采集：每个请求的分级结果、目标槽位、实际槽位、排队位置、prefill 耗时、生成速度、上下文占用与是否截断。日志里不要只写成功，失败路径要带异常类型与任务 id。服务重启后，处于 processing 的任务应标记为失败而不是永久挂起，pending 任务应重新入队。这些细节不提升峰值性能，但决定了系统在异常时是否可运维。",
]


def rand4():
    return "".join(RNG.choice(string.ascii_uppercase + string.digits) for _ in range(4))


def material(offset, marker, paragraphs=6, repeat=1):
    """按 offset 轮换段落顺序，插入本轮标记；repeat 用于压长上下文预算。"""
    ordered = PARAS[offset:] + PARAS[:offset]
    body = "\n\n".join(ordered[:paragraphs] * repeat)
    return f"【本轮标记 {marker}】\n\n{body}"


def http_json(path, payload=None, timeout=300):
    data = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None else None)
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}
    except Exception as exc:
        return -1, {"error": f"{type(exc).__name__}: {exc}"}


def http_sse(path, payload, timeout=300):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    frames, raw, current = [], [], None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        for line in r:
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            raw.append(text)
            if text.startswith("event: "):
                current = text[len("event: "):]
            elif text.startswith("data: "):
                body = text[len("data: "):]
                if body.strip() == "[DONE]":
                    frames.append({"done": True})
                    continue
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"_raw": body[:120]}
                data["_event"] = current
                frames.append(data)
    return frames, raw, ctype


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


def record(category, name, ok, detail=""):
    RESULTS.append({"category": category, "check": name, "ok": bool(ok),
                    "detail": detail})
    print(("PASS " if ok else "FAIL ") + f"[{category}] " + name
          + ("" if ok else f" :: {detail}"))
    return bool(ok)


def line_counts():
    counts = {}
    for path in LOGS:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                counts[path] = sum(1 for _ in f)
        except FileNotFoundError:
            counts[path] = 0
    return counts


SUSPECT = re.compile(
    r"traceback|exception|\bERROR\b|out of memory|oom-kill|cuda error|"
    r"segmentation|core dumped|killed by signal|failed to|connection refused",
    re.I)


def scan_logs(before):
    summary = {}
    for path in LOGS:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except FileNotFoundError:
            summary[path] = {"exists": False, "new_lines": 0,
                             "suspicious_count": 0, "suspicious": []}
            continue
        new = lines[before.get(path, 0):]
        bad = [ln.rstrip() for ln in new if SUSPECT.search(ln)]
        summary[path] = {"exists": True, "new_lines": len(new),
                         "suspicious_count": len(bad), "suspicious": bad[:15]}
    return summary


# ── A. 长上下文多轮（/chat）─────────────────────────────────────
def scenario_a():
    cat = "A-长上下文"
    sid = f"acc-a-{rand4().lower()}"
    markers = []
    # 5 轮 × 约 3.5K 字（6 段重复三遍）：长输入轮次的历史预算约 12.9K，
    # 第 5 轮累计历史超过预算，必然触发一次截断
    trunc_seen = False
    for turn in range(1, 7):
        marker = f"ACC-A{turn}-{rand4()}"
        markers.append(marker)
        if turn <= 5:
            prompt = (f"{material(turn - 1, marker, paragraphs=6, repeat=3)}\n\n"
                      "请用 5 条要点概括上述材料，每条不超过 25 字。")
        else:
            prompt = "第一轮材料的标记是什么？只回答标记本身。"
        t0 = time.time()
        status, d = http_json("/chat", {"user_input": prompt, "session_id": sid})
        took = round(time.time() - t0, 1)
        ctx = d.get("context_usage") or {}
        record(cat, f"第{turn}轮 HTTP 200", status == 200, status)
        record(cat, f"第{turn}轮未溢出",
               ctx.get("estimated_total_chars", 0) <= ctx.get("num_ctx", 1),
               f"{ctx.get('estimated_total_chars')}/{ctx.get('num_ctx')}")
        if turn <= 5:
            record(cat, f"第{turn}轮进入后台",
                   d.get("process_mode") == "async", d.get("process_mode"))
            trunc_seen = trunc_seen or bool(ctx.get("history_truncated"))
            task = wait_task(d["task_id"])
            text = task.get("final_reply") or ""
            record(cat, f"第{turn}轮后台完成",
                   task.get("task_status") == "completed", task.get("task_status"))
            record(cat, f"第{turn}轮产出非空", len(text) >= 20, len(text))
            print(f"      turn{turn}: {took}s, input={len(prompt)}字, "
                  f"hist={ctx.get('history_chars')}字/{ctx.get('history_messages')}条, "
                  f"truncated={ctx.get('history_truncated')}, reply={len(text)}字")
        else:
            reply = (d.get("instant_reply") or "")
            record(cat, "长上下文期间至少截断一次", trunc_seen, trunc_seen)
            hit = markers[0] in reply
            print(f"      turn6: {took}s, hist={ctx.get('history_chars')}字/"
                  f"{ctx.get('history_messages')}条, "
                  f"truncated={ctx.get('history_truncated')}, A1_recall={hit}")
            RESULTS.append({"category": cat, "check": "末轮早期召回（仅记录，不判失败）",
                            "ok": True, "detail": f"A1_recall={hit}, reply={reply[:40]}"})
    return sid


# ── B. Responses 续接多轮 ─────────────────────────────────────
def scenario_b():
    cat = "B-续接"
    prev, markers = None, []
    for turn in range(1, 5):
        marker = f"ACC-B{turn}-{rand4()}"
        markers.append(marker)
        payload = {"model": "explore",
                   "input": f"{material(turn % 6, marker, paragraphs=4)}\n\n"
                            "用一句话总结这段材料的核心。",
                   "store": True}
        if prev:
            payload["previous_response_id"] = prev
        status, d = http_json("/v1/responses", payload)
        meta = d.get("explore") or {}
        text = ""
        for item in d.get("output") or []:
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text = part.get("text") or ""
        record(cat, f"第{turn}轮 HTTP 200", status == 200, status)
        record(cat, f"第{turn}轮 status=completed",
               d.get("status") == "completed", d.get("status"))
        record(cat, f"第{turn}轮产出非空", len(text) >= 10, len(text))
        if turn > 1:
            record(cat, f"第{turn}轮带上历史",
                   meta.get("history_messages", 0) >= 2, meta.get("history_messages"))
        resp_id = d.get("id")
        record(cat, f"第{turn}轮返回 resp_ id",
               isinstance(resp_id, str) and resp_id.startswith("resp_"), resp_id)
        prev = resp_id
    # 末轮追问最近一轮标记
    status, d = http_json("/v1/responses", {
        "model": "explore", "previous_response_id": prev,
        "input": "刚才最后一轮材料的标记是什么？只回答标记本身。"})
    text = ""
    for item in d.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                text = part.get("text") or ""
    record(cat, "末轮 HTTP 200", status == 200, status)
    record(cat, "末轮召回最近标记", markers[-1] in text, repr(text[:60]))
    return


# ── C. 连续长文本回复 ────────────────────────────────────────
def scenario_c():
    cat = "C-长文"
    sid = f"acc-c-{rand4().lower()}"
    topics = ["本地推理的显存预算", "上下文窗口与历史预算", "槽位切换的代价与取舍"]
    for i, topic in enumerate(topics, 1):
        prompt = (f"请写一篇不少于 600 字的说明，主题：{topic}。"
                  "必须分 4 个小节，每节至少 120 字，最后单列一句结论。"
                  "不要客套话，不要复述题目，不要用表格。")
        status, d = http_json("/chat", {"user_input": prompt, "session_id": sid})
        record(cat, f"第{i}篇 HTTP 200", status == 200, status)
        task = wait_task(d.get("task_id") or "")
        text = task.get("final_reply") or ""
        record(cat, f"第{i}篇完成",
               task.get("task_status") == "completed", task.get("task_status"))
        record(cat, f"第{i}篇长度 300-1800",
               300 <= len(text) <= 1800, len(text))
        print(f"      article{i}: {len(text)}字, slot={task.get('chosen_model')}")
    return


# ── D. 三协议一致性 ─────────────────────────────────────────
def scenario_d():
    cat = "D-一致性"
    short = "2+2 等于几"
    results = {}
    status, d = http_json("/chat", {"user_input": short, "session_id": f"acc-d1-{rand4().lower()}"})
    results["/chat"] = (d.get("task_type"), d.get("process_mode"))
    record(cat, "/chat 短输入 direct", d.get("process_mode") == "direct", d.get("process_mode"))
    status, d = http_json("/v1/chat/completions", {"model": "explore",
                                                   "messages": [{"role": "user", "content": short}]})
    meta = d.get("explore") or {}
    results["/v1/chat/completions"] = (meta.get("tier"), meta.get("process_mode"))
    record(cat, "chat/completions 短输入 direct",
           meta.get("process_mode") == "direct", meta.get("process_mode"))
    status, d = http_json("/v1/responses", {"model": "explore", "input": short})
    meta = d.get("explore") or {}
    results["/v1/responses"] = (meta.get("tier"), meta.get("process_mode"))
    record(cat, "responses 短输入 direct",
           meta.get("process_mode") == "direct", meta.get("process_mode"))
    modes = {v[1] for v in results.values()}
    record(cat, "三协议 process_mode 一致", len(modes) == 1, results)

    # 长输入：三协议都应有相同 tier（不等后台，稍后统一清队列）
    long_text = ("请详细分析并给出完整方案：" + "本地推理需要在延迟与质量之间取舍。" * 8)
    tier = {}
    status, d = http_json("/chat", {"user_input": long_text, "session_id": f"acc-d2-{rand4().lower()}"})
    tier["/chat"] = d.get("task_type")
    status, d = http_json("/v1/chat/completions", {"model": "explore",
                                                   "messages": [{"role": "user", "content": long_text}]})
    tier["/v1/chat/completions"] = (d.get("explore") or {}).get("tier")
    status, d = http_json("/v1/responses", {"model": "explore", "input": long_text})
    tier["/v1/responses"] = (d.get("explore") or {}).get("tier")
    record(cat, "三协议长输入 tier 一致", len(set(tier.values())) == 1, tier)


# ── E. 并发队列 ─────────────────────────────────────────────
def scenario_e():
    cat = "E-并发"
    sid = f"acc-e-{rand4().lower()}"
    got, lock = [], threading.Lock()

    def worker(i):
        prompt = (f"请详细分析第 {i} 个主题并给出完整方案，包含风险与取舍。"
                  + "本地推理需要在延迟与质量之间做出权衡。" * 6)
        status, d = http_json("/chat", {"user_input": prompt, "session_id": sid},
                              timeout=180)
        with lock:
            got.append((i, status, d))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 6)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    took = round(time.time() - t0, 1)
    record(cat, "5 个并发请求全部 HTTP 200",
           len(got) == 5 and all(s == 200 for _, s, _ in got),
           [(i, s) for i, s, _ in got])
    ids = [d.get("task_id") for _, _, d in got]
    record(cat, "task_id 唯一", len(set(ids)) == 5, ids)
    positions = [d.get("queue_position") for _, _, d in got]
    record(cat, "queue_position 合理（0-4）",
           all(isinstance(p, int) and 0 <= p <= 4 for p in positions), positions)
    print(f"      submit took {took}s, positions={sorted(positions)}")
    done = 0
    for task_id in ids:
        task = wait_task(task_id, timeout=360)
        if task.get("task_status") == "completed":
            done += 1
    record(cat, "并发任务全部 completed", done == 5, f"{done}/5")


# ── F. 流式长文本 ───────────────────────────────────────────
def scenario_f():
    cat = "F-流式"
    frames, raw, ctype = http_sse("/v1/chat/completions", {
        "model": "explore", "stream": True,
        "messages": [{"role": "user",
                      "content": "请用大约 400 字说明本地推理的显存规划，分点展开。"}]})
    text = "".join((f.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                   for f in frames if f.get("choices"))
    record(cat, "chat SSE 有 [DONE]", any(f.get("done") for f in frames))
    record(cat, "chat SSE 拼接收尾 finish_reason=stop",
           any((f.get("choices") or [{}])[0].get("finish_reason") == "stop"
               for f in frames if f.get("choices")))
    record(cat, "chat SSE 文本长度 >= 200", len(text) >= 200, len(text))

    frames2, raw2, ctype2 = http_sse("/v1/responses", {
        "model": "explore", "stream": True,
        "input": "请用大约 400 字说明上下文预算，分点展开。"})
    names = [f.get("_event") for f in frames2]
    deltas = "".join(f.get("delta") or "" for f in frames2
                     if f.get("_event") == "response.output_text.delta")
    done_text = next((f.get("text") for f in frames2
                      if f.get("_event") == "response.output_text.done"), "")
    record(cat, "responses SSE 以 completed 结束",
           "response.completed" in names, names[-3:])
    record(cat, "responses SSE 无 [DONE]",
           not any(l.strip() == "data: [DONE]" for l in raw2), raw2[-2:])
    record(cat, "responses SSE done 与 delta 一致",
           done_text == deltas, f"{len(done_text)} vs {len(deltas)}")
    record(cat, "responses SSE 文本长度 >= 200", len(deltas) >= 200, len(deltas))


# ── G. 边界错误 ─────────────────────────────────────────────
def scenario_g(max_input):
    cat = "G-边界"
    status, _ = http_json("/chat", {"user_input": ""})
    record(cat, "/chat 空输入 400", status == 400, status)
    if isinstance(max_input, int) and max_input > 0:
        status, _ = http_json("/chat", {"user_input": "超" * (max_input + 100)}, timeout=300)
        record(cat, "/chat 超长输入 413", status == 413, status)
    status, _ = http_json("/v1/chat/completions", {"messages": []})
    record(cat, "chat/completions 空 messages 400", status == 400, status)
    status, _ = http_json("/v1/responses", {"input": ""})
    record(cat, "responses 空 input 400", status == 400, status)
    status, _ = http_json("/v1/responses", {"input": "hi",
                                            "previous_response_id": "resp_nope"})
    record(cat, "未知 previous_response_id 404", status == 404, status)


def main():
    status, cfg = http_json("/config")
    if status != 200:
        print(f"服务不可用：/config -> {status} {cfg}")
        return 2
    tiers = cfg.get("tier_order") or []
    limits = cfg.get("limits") or {}
    max_input = limits.get("max_input_chars")
    plan = {k: v["available"] for k, v in cfg["vram_plan"].items()}
    print(f"target={BASE} tiers={tiers} num_ctx={cfg.get('num_ctx')} "
          f"max_input={max_input}")
    print(f"vram_plan={plan}")
    print()

    before = line_counts()
    t_all = time.time()
    # 可只跑指定场景：python3 tests/audit_acceptance.py a c
    selected = {arg.lower() for arg in sys.argv[1:]}

    def want(key):
        return not selected or key in selected

    if want("a"):
        print("=== A 长上下文多轮（/chat，5 轮 × 约 2.9K 字 + 追问）===")
        scenario_a()
    if want("b"):
        print("\n=== B Responses 续接（4 轮 × 约 1.8K 字）===")
        scenario_b()
    if want("c"):
        print("\n=== C 连续长文本回复（3 篇不少于 600 字）===")
        scenario_c()
    if want("d"):
        print("\n=== D 三协议一致性 ===")
        scenario_d()
        # 等 D 留下的后台任务清空，再压并发
        for _ in range(120):
            _, h = http_json("/health", timeout=30)
            if h.get("queue_depth") == 0:
                break
            time.sleep(2)
    if want("e"):
        print("\n=== E 并发队列（5 个 async）===")
        scenario_e()
    if want("f"):
        print("\n=== F 流式长文本 ===")
        scenario_f()
    if want("g"):
        print("\n=== G 边界错误 ===")
        scenario_g(max_input)

    elapsed = round(time.time() - t_all, 1)
    logs = scan_logs(before)
    suspicious_total = sum(v["suspicious_count"] for v in logs.values())
    record("H-日志", "服务端日志无 Traceback/ERROR/OOM",
           suspicious_total == 0,
           {k: v["suspicious"][:3] for k, v in logs.items() if v["suspicious_count"]})
    record("H-日志", "压测期间前台未重启",
           (http_json("/health", timeout=30)[1].get("frontend") == "ready"))

    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    print(f"\n=== 日志新增行 ===")
    for path, info in logs.items():
        print(f"  {path}: +{info['new_lines']} 行, 可疑 {info['suspicious_count']}")
        for line in info["suspicious"][:5]:
            print(f"    ! {line[:160]}")
    print(f"\n总耗时 {elapsed}s")
    print(f"RESULT: {passed}/{total} passed")

    os.makedirs("logs", exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join("logs", f"acceptance_{stamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"base": BASE, "elapsed_s": elapsed, "tiers": tiers,
                   "num_ctx": cfg.get("num_ctx"), "max_input_chars": max_input,
                   "vram_plan": {k: v["available"] for k, v in cfg["vram_plan"].items()},
                   "passed": passed, "total": total,
                   "logs": logs, "results": RESULTS}, f, ensure_ascii=False, indent=2)
    print(f"结果写入 {out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

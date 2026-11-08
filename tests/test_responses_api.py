"""OpenAI Responses API 真机测试：全部走真实 HTTP 与真实模型，不使用 mock。

用法（在已启动服务的工作区根目录）：
    python3 tests/test_responses_api.py

覆盖：非流式 direct/instant/final、流式完整事件序列、previous_response_id 多轮续接、
GET 检索、store=false 不落库、错误路径。

结果：logs/responses_api_test.json；stdout 打印摘要；退出码非 0 表示有失败项。
BASE 环境变量可覆盖地址，默认 http://127.0.0.1:8100。
"""
import json
import os
import time
import urllib.error
import urllib.request

BASE = os.getenv("BASE", "http://127.0.0.1:8100")
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" :: {detail}"))
    return bool(ok)


def http_json(path, payload=None, timeout=240):
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
            return e.code, {"raw": raw[:400]}
    except Exception as exc:
        return -1, {"error": f"{type(exc).__name__}: {exc}"}


def http_sse(path, payload, timeout=300):
    """读取 Responses 的 SSE：返回 (events, raw_lines, content_type)。

    每个 event 附带 `_event`（event: 行的值）便于断言事件类型序列。
    """
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    events, raw = [], []
    current = None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        for line in r:
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            raw.append(text)
            if text.startswith("event: "):
                current = text[len("event: "):]
            elif text.startswith("data: "):
                body = text[len("data: "):]
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"_raw": body[:120]}
                data["_event"] = current
                events.append(data)
    return events, raw, ctype


def output_text(payload):
    """从 response 对象里取第一段 output_text。"""
    for item in payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                return part.get("text") or ""
    return ""


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


def main():
    status, cfg = http_json("/config")
    if status != 200:
        print(f"服务不可用：/config 返回 {status} {cfg}")
        return 2
    tiers = cfg.get("tier_order") or ["simple", "mid", "heavy"]
    heavy = tiers[-1]
    openai_cfg = cfg.get("openai") or {}
    print(f"target={BASE} tiers={tiers} openai={openai_cfg}\n")

    # 1) 非流式 direct
    status, d = http_json("/v1/responses", {
        "model": "explore",
        "input": "2+2 等于几？只回答数字。",
    })
    text = output_text(d)
    meta = d.get("explore") or {}
    record("direct HTTP 200", status == 200, status)
    record("direct object=response", d.get("object") == "response", d.get("object"))
    record("direct status=completed", d.get("status") == "completed", d.get("status"))
    record("direct output[0] 为 message + output_text",
           bool(d.get("output")) and d["output"][0]["type"] == "message"
           and d["output"][0]["content"][0]["type"] == "output_text",
           d.get("output"))
    record("direct 正文非空", bool(text.strip()), repr(text[:60]))
    record("direct process_mode=direct",
           meta.get("process_mode") == "direct", meta.get("process_mode"))
    record("direct usage 字段", (d.get("usage") or {}).get("input_tokens", 0) > 0
           and (d.get("usage") or {}).get("total_tokens", 0) > 0, d.get("usage"))

    # 2) final：等后台完成
    long_text = ("请详细分析并给出完整方案：本地模型路由需要在显存、上下文长度、"
                 "响应速度与结果质量之间取得平衡。" * 6)
    status, fin = http_json("/v1/responses", {
        "model": f"explore-{heavy}",
        "input": long_text,
    })
    fin_text = output_text(fin)
    meta_f = fin.get("explore") or {}
    record("final HTTP 200", status == 200, status)
    record("final status=completed", fin.get("status") == "completed", fin.get("status"))
    record("final process_mode=async",
           meta_f.get("process_mode") == "async", meta_f.get("process_mode"))
    record("final tier 命中", meta_f.get("tier") == heavy, meta_f.get("tier"))
    record("final 正文非空", bool(fin_text.strip()), repr(fin_text[:60]))
    if meta_f.get("task_id"):
        _, own = http_json(f"/task_result?task_id={meta_f['task_id']}", timeout=30)
        record("final 正文与任务 final_reply 逐字一致",
               fin_text == (own.get("final_reply") or ""),
               f"{len(fin_text)} vs {len(own.get('final_reply') or '')}")

    # 2.1) 截断可见性：撞 max_tokens 时必须报 incomplete，不能谎称 completed
    status, trunc = http_json("/v1/responses", {
        "model": "explore",
        "input": "请写不少于800字，分8个小节说明显存预算"})
    record("截断时 status=incomplete",
           status == 200 and trunc.get("status") == "incomplete",
           (status, trunc.get("status")))
    record("截断原因 max_output_tokens",
           (trunc.get("incomplete_details") or {}).get("reason") == "max_output_tokens",
           trunc.get("incomplete_details"))
    record("截断时 explore.finish_reason=length",
           (trunc.get("explore") or {}).get("finish_reason") == "length",
           (trunc.get("explore") or {}).get("finish_reason"))

    # 3) instant：立即返回 + task_id
    status, inst = http_json("/v1/responses", {
        "model": "explore-fast",
        "input": long_text,
    })
    meta_i = inst.get("explore") or {}
    inst_text = output_text(inst)
    record("instant HTTP 200", status == 200, status)
    record("instant status=completed", inst.get("status") == "completed", inst.get("status"))
    record("instant process_mode=async",
           meta_i.get("process_mode") == "async", meta_i.get("process_mode"))
    record("instant 给出 task_id", bool(meta_i.get("task_id")), meta_i.get("task_id"))
    record("instant 正文非空", bool(inst_text.strip()), repr(inst_text[:60]))
    if meta_i.get("task_id"):
        task = wait_task(meta_i["task_id"])
        record("instant 后台任务最终 completed",
               task.get("task_status") == "completed", task.get("task_status"))

    # 4) 流式：事件序列
    events, raw, ctype = http_sse("/v1/responses", {
        "model": "explore",
        "input": "你好",
        "stream": True,
    })
    names = [e.get("_event") for e in events]
    deltas = "".join(e.get("delta") or "" for e in events
                     if e.get("_event") == "response.output_text.delta")
    record("stream content-type=text/event-stream",
           "text/event-stream" in ctype, ctype)
    record("stream 有 event: 行", any(l.startswith("event: response.created") for l in raw),
           raw[:2])
    record("stream 以 response.created 开头",
           names and names[0] == "response.created", names[:3])
    record("stream 含 output_text.delta 与 completed",
           "response.output_text.delta" in names
           and "response.completed" in names, names[-6:])
    record("stream 事件顺序正确",
           names.index("response.completed") > names.index("response.output_text.done")
           if "response.output_text.done" in names and "response.completed" in names
           else False, names)
    record("stream 拼接正文非空", bool(deltas.strip()), repr(deltas[:60]))
    record("stream 不以 [DONE] 结尾", not any(l.strip() == "data: [DONE]" for l in raw),
           raw[-2:])
    done_text = next((e.get("text") for e in events
                      if e.get("_event") == "response.output_text.done"), "")
    record("stream output_text.done 与 delta 拼接一致",
           done_text == deltas, f"{len(done_text)} vs {len(deltas)}")

    # 5) previous_response_id：多轮续接
    marker = "RESP-7Q2K"
    status, first = http_json("/v1/responses", {
        "model": "explore",
        "input": f"记住暗号 {marker}。只回复：收到",
        "store": True,
    })
    first_id = first.get("id")
    first_text = output_text(first)
    record("多轮第一轮成功", status == 200 and bool(first_id), (status, first_id))
    record("第一轮返回 resp_ 前缀 id",
           isinstance(first_id, str) and first_id.startswith("resp_"), first_id)

    if first_id:
        status, second = http_json("/v1/responses", {
            "model": "explore",
            "input": "刚才的暗号是什么？只回答暗号本身。",
            "previous_response_id": first_id,
        })
        second_text = output_text(second)
        meta_s = second.get("explore") or {}
        record("多轮第二轮 HTTP 200", status == 200, status)
        record("续接带上了历史", meta_s.get("history_messages", 0) >= 2,
               meta_s.get("history_messages"))
        record("第二轮召回暗号", marker in second_text, repr(second_text[:80]))

        # 6) GET 检索
        status, fetched = http_json(f"/v1/responses/{first_id}")
        record("GET /v1/responses/{id} -> 200", status == 200, status)
        record("GET 内容与首次返回一致",
               output_text(fetched) == first_text,
               f"{len(output_text(fetched))} vs {len(first_text)}")

    # 7) store=false 不落库
    status, ephemeral = http_json("/v1/responses", {
        "model": "explore",
        "input": "临时请求",
        "store": False,
    })
    eph_id = ephemeral.get("id")
    record("store=false 请求成功", status == 200 and bool(eph_id), (status, eph_id))
    if eph_id:
        status, _ = http_json(f"/v1/responses/{eph_id}")
        record("store=false 后 GET -> 404", status == 404, status)

    # 8) 错误路径
    status, err = http_json("/v1/responses", {"input": ""})
    record("空 input -> 400", status == 400
           and (err.get("error") or {}).get("param") == "input", (status, err))
    status, err = http_json("/v1/responses", {
        "input": "hi", "previous_response_id": "resp_not_exist"})
    record("未知 previous_response_id -> 404",
           status == 404
           and (err.get("error") or {}).get("code") == "previous_response_not_found",
           (status, err))

    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    print(f"\nRESULT: {passed}/{total} passed")
    os.makedirs("logs", exist_ok=True)
    with open(os.path.join("logs", "responses_api_test.json"), "w", encoding="utf-8") as f:
        json.dump({"base": BASE, "tiers": tiers, "openai": openai_cfg,
                   "passed": passed, "total": total, "results": RESULTS},
                  f, ensure_ascii=False, indent=2)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

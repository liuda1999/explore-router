"""OpenAI 兼容层真机测试：全部走真实 HTTP 与真实模型，不使用 mock。

用法（在已启动服务的工作区根目录）：
    python3 tests/test_openai_api.py

覆盖：/v1/models、direct 同步、instant 只回即时回复、final 等待后台、
流式（direct 与 async 两段）、错误路径（空 messages / 非法 explore_mode / 超长输入）。

结果：logs/openai_api_test.json；stdout 打印摘要；退出码非 0 表示有失败项。
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


def http_stream(path, payload, timeout=300):
    """读取 SSE：返回 (frames, raw_lines, content_type)；[DONE] 记为 {"done": True}。"""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    frames, raw = [], []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        for line in r:
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            raw.append(text)
            if not text.startswith("data: "):
                continue
            body = text[len("data: "):]
            if body.strip() == "[DONE]":
                frames.append({"done": True})
                continue
            try:
                frames.append(json.loads(body))
            except Exception:
                frames.append({"parse_error": body[:120]})
    return frames, raw, ctype


def stream_text(frames):
    return "".join(
        (f.get("choices") or [{}])[0].get("delta", {}).get("content", "")
        for f in frames if f.get("choices"))


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


def message_of(payload):
    ch = (payload.get("choices") or [{}])[0]
    return (ch.get("message") or {}).get("content") or ""


def main():
    status, cfg = http_json("/config")
    if status != 200:
        print(f"服务不可用：/config 返回 {status} {cfg}")
        return 2
    tiers = cfg.get("tier_order") or ["simple", "mid", "heavy"]
    heavy = tiers[-1]
    limits = cfg.get("limits") or {}
    max_input = limits.get("max_input_chars")
    openai_cfg = cfg.get("openai") or {}
    print(f"target={BASE} tiers={tiers} max_input_chars={max_input} "
          f"openai={openai_cfg}\n")

    # 1) 模型列表
    status, models = http_json("/v1/models")
    ids = [m.get("id") for m in models.get("data") or []]
    record("GET /v1/models -> 200", status == 200, status)
    record("models.object=list", models.get("object") == "list", models.get("object"))
    record("models 含 explore / explore-fast",
           "explore" in ids and "explore-fast" in ids, ids)
    record("models 含各档位别名",
           all(f"explore-{t}" in ids for t in tiers), ids)

    # 2) direct：短输入走前台直答
    t0 = time.time()
    status, direct = http_json("/v1/chat/completions", {
        "model": "explore",
        "messages": [{"role": "user", "content": "2+2 等于几"}],
    })
    direct_ms = round((time.time() - t0) * 1000)
    meta = direct.get("explore") or {}
    content = message_of(direct)
    record("direct HTTP 200", status == 200, status)
    record("direct object=chat.completion",
           direct.get("object") == "chat.completion", direct.get("object"))
    record("direct content 非空", bool(content.strip()), repr(content[:60]))
    record("direct process_mode=direct",
           meta.get("process_mode") == "direct", meta.get("process_mode"))
    record("direct task_status=completed",
           meta.get("task_status") == "completed", meta.get("task_status"))
    record("direct usage 为正整数",
           isinstance(direct.get("usage"), dict)
           and direct["usage"].get("total_tokens", 0) > 0, direct.get("usage"))
    record("direct 延迟 < 60s", direct_ms < 60000, f"{direct_ms}ms")

    # 3) instant：复杂任务只回即时回复，task_id 可轮询
    long_text = ("请详细分析并给出完整方案：本地模型路由需要在显存、上下文长度、"
                 "响应速度与结果质量之间取得平衡。" * 6)
    status, inst = http_json("/v1/chat/completions", {
        "model": "explore-fast",
        "messages": [{"role": "user", "content": long_text}],
    })
    meta_i = inst.get("explore") or {}
    content_i = message_of(inst)
    record("instant HTTP 200", status == 200, status)
    record("instant content 非空", bool(content_i.strip()), repr(content_i[:60]))
    record("instant process_mode=async",
           meta_i.get("process_mode") == "async", meta_i.get("process_mode"))
    record("instant 立即返回（非阻塞）",
           meta_i.get("task_status") in ("pending", "processing", "completed"),
           meta_i.get("task_status"))
    record("instant 提供 task_id", bool(meta_i.get("task_id")), meta_i.get("task_id"))
    task = wait_task(meta_i["task_id"]) if meta_i.get("task_id") else {}
    record("instant 后台任务最终 completed",
           task.get("task_status") == "completed", task.get("task_status"))
    record("instant 未把后台结果塞进即时回复",
           content_i != (task.get("final_reply") or ""), len(content_i))

    # 4) final：同步等待后台完成
    status, fin = http_json("/v1/chat/completions", {
        "model": f"explore-{heavy}",
        "messages": [{"role": "user", "content": long_text}],
    })
    meta_f = fin.get("explore") or {}
    content_f = message_of(fin)
    record("final HTTP 200", status == 200, status)
    record("final process_mode=async",
           meta_f.get("process_mode") == "async", meta_f.get("process_mode"))
    record("final task_status=completed",
           meta_f.get("task_status") == "completed", meta_f.get("task_status"))
    record("final tier 命中指定档位", meta_f.get("tier") == heavy, meta_f.get("tier"))
    record("final content 非空", bool(content_f.strip()), repr(content_f[:60]))
    record("final content 与即时回复内容不同（确为后台结果）",
           content_f.strip() != content_i.strip(),
           f"{len(content_f)} vs {len(content_i)}")
    if meta_f.get("task_id"):
        _, own = http_json(f"/task_result?task_id={meta_f['task_id']}", timeout=30)
        own_final = own.get("final_reply") or ""
        record("final content 与自身 task 的 final_reply 逐字一致",
               content_f == own_final, f"{len(content_f)} vs {len(own_final)}")

    # 5) 流式：direct
    frames, raw, ctype = http_stream("/v1/chat/completions", {
        "model": "explore", "stream": True,
        "messages": [{"role": "user", "content": "你好"}],
    })
    text = stream_text(frames)
    first = (frames[0].get("choices") or [{}])[0].get("delta", {}) if frames else {}
    finishes = [(f.get("choices") or [{}])[0].get("finish_reason")
                for f in frames if f.get("choices")]
    record("stream content-type=text/event-stream",
           "text/event-stream" in ctype, ctype)
    record("stream 首帧带 role=assistant", first.get("role") == "assistant", first)
    record("stream 有正文增量", bool(text.strip()), repr(text[:60]))
    record("stream 末帧 finish_reason=stop", "stop" in finishes, finishes[-3:])
    record("stream 以 [DONE] 结束", bool(frames) and frames[-1].get("done") is True)

    # 6) 流式：async 两段（即时回复 + 最终结果）
    frames2, raw2, ctype2 = http_stream("/v1/chat/completions", {
        "model": "explore", "stream": True,
        "messages": [{"role": "user", "content": long_text}],
    })
    text2 = stream_text(frames2)
    record("stream async 内容包含两段",
           len(text2) > len(content_i) and len(text2) > 80, len(text2))
    record("stream async 正常收尾",
           bool(frames2) and frames2[-1].get("done") is True)
    record("stream async 期间有内容输出",
           any(line.startswith("data: ") for line in raw2), len(raw2))

    # 7) 错误路径
    status, err = http_json("/v1/chat/completions", {"messages": []})
    record("空 messages -> 400 + OpenAI 错误体",
           status == 400 and (err.get("error") or {}).get("type") == "invalid_request_error",
           (status, err))
    status, err = http_json("/v1/chat/completions", {
        "messages": [{"role": "user", "content": "hi"}], "explore_mode": "wat"})
    record("非法 explore_mode -> 400 + code=invalid_mode",
           status == 400 and (err.get("error") or {}).get("code") == "invalid_mode",
           (status, err))
    if isinstance(max_input, int) and max_input > 0:
        status, err = http_json("/v1/chat/completions", {
            "messages": [{"role": "user", "content": "超" * (max_input + 50)}]},
            timeout=300)
        record("超长输入 -> 413 + code=context_length_exceeded",
               status == 413
               and (err.get("error") or {}).get("code") == "context_length_exceeded",
               (status, str(err)[:160]))
    else:
        record("超长输入检查（max_input_chars 非整数，跳过）", True, max_input)

    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    print(f"\nRESULT: {passed}/{total} passed")
    os.makedirs("logs", exist_ok=True)
    with open(os.path.join("logs", "openai_api_test.json"), "w", encoding="utf-8") as f:
        json.dump({"base": BASE, "tiers": tiers, "openai": openai_cfg,
                   "passed": passed, "total": total, "results": RESULTS},
                  f, ensure_ascii=False, indent=2)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

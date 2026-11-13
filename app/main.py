"""explore 智能本地路由（配置驱动版）。

前台：由 settings.yaml 中 resident=true 的槽位常驻，句句有着落、即时回复、
      简单任务直接完成、进度应答；本地前台进程异常时自动重启一次，
      api 前台（线上模型）无进程，连通性由调用本身兜底。
后台：本地互斥槽位默认常驻一个模型，需要另一档时才切换；type: api 槽位
      指向 OpenAI 兼容线上模型，无显存约束、不参与互斥切换；结果原样递回。
配置：槽位（local/api）、模型、硬件、上下文、端口、限额全部来自 settings.yaml；
      运行期参数可经 /admin/config 热调并持久化到 settings.local.yaml。
状态：SQLite 持久化；重启时恢复未完成任务，processing 标记失败避免卡死。
"""
import asyncio
import copy
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import compaction, config, gpu, openai_api, prompts, store
from .api_backend import create_backend
from .console import console_page
from .embed import EmbeddingProvider
from .router import SlotManager, classify, slot_for_tier

# 强进度词：明确在问任务状态；弱进度词：需要同时存在进行中的任务才算进度提问
STRONG_PROGRESS_WORDS = ("进度", "到哪了", "到哪一步", "完成了吗", "任务状态",
                         "处理完", "还在吗", "status")
WEAK_PROGRESS_WORDS = ("好了吗", "怎么样了", "怎么样")

# 设了 ADMIN_TOKEN 时，POST/DELETE /admin/* 需带 X-Admin-Token 头（GET 不限）
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

front = None        # LlamaServer（local 槽位）或 ApiBackend（api 槽位），由工厂创建
slot: SlotManager | None = None
embedding: EmbeddingProvider | None = None
queue: asyncio.Queue | None = None
worker_task = None
front_lock = asyncio.Lock()
# 前台实例被即时回复与压缩摘要并发调用；串行化避免 last_finish_reason/last_timings
# 共享属性被交叉覆写（正文内容不受影响，元数据会张冠李戴）
front_chat_lock = asyncio.Lock()
# 每会话压缩锁：同会话并发请求不重复摘要、covered_messages 不回退
_compact_locks: dict[str, asyncio.Lock] = {}
front_last_restart = 0.0
# /admin/restart 防重入：守护子进程已派出后拒绝再次重启
RESTARTING = False


async def ensure_front():
    """前台是常驻交互入口：本地进程不健康时重启一次，冷却期内不反复重启。

    api 前台没有进程可重启，直接放行——连通性问题由 chat 调用本身暴露，
    失败走 prompts.front_failure 降级文案，不影响请求返回。
    """
    global front_last_restart
    if front is None:
        return False
    if config.FRONT_TYPE == "api":
        return True
    if await front.healthy():
        return True
    async with front_lock:
        if await front.healthy():
            return True
        if time.time() - front_last_restart < config.FRONT_RESTART_COOLDOWN_S:
            return False
        front_last_restart = time.time()
        await front.stop()
        await front.start()
        return True


async def process_task(task_id):
    t = store.get_task(task_id)
    if not t:
        return
    tier = t["tier"] or config.TIER_ORDER[1]
    target = config.TIER_TO_SLOT.get(tier) or config.DEFAULT_SLOT
    slot_key = slot_for_tier(tier)
    # 后台槽位在排队期间被其他进程挤占显存时，直接由前台结果收尾，避免再起一个前台实例
    if slot_key == config.FRONT_KEY:
        store.update_task(task_id, status="completed", progress=100,
                          final_reply=t.get("instant_reply") or "",
                          release_info="后台槽位显存不足，由前台直接完成")
        return
    degraded = slot_key != target
    degrade_note = (f"；{target} 显存不足，已降级到 {slot_key}"
                    if degraded else "")
    start_note = (f"{target} 显存不足，已降级到 {slot_key}"
                  if degraded else f"waiting for slot / loading {slot_key}")
    store.update_task(task_id, status="processing", progress=10,
                      model_name=slot_key, release_info=start_note)
    t0 = time.time()
    try:
        server, switched = await slot.ensure(slot_key)
        if switched:
            store.update_task(task_id, progress=20,
                              release_info=(f"slot switched; load {server.load_ms}ms"
                                            f"{degrade_note}"))
        # 短任务包：只给后台模型必要信息，不灌完整会话历史
        # 超长输入按「该槽位」的上下文预算截断（api 槽位用自己的窗口）；
        # 截断策略：按段落/句子边界保留完整单元，切点不落在半句话上
        task_text, truncated = prompts.truncate_for_task(
            t["user_input"],
            max(512, config.slot_ctx(slot_key)
                - config.slot_max_tokens(slot_key)
                - config.CONTEXT_SAFETY_TOKENS))
        # 统一模板：system + user 两条，字段顺序固定，见 app/prompts.py
        # 嵌入可能是同步 HTTP（ollama provider）：放线程池避免阻塞事件循环
        clues = await asyncio.to_thread(embedding.split, task_text)
        messages = prompts.backend_messages(task_text, clues, truncated=truncated)
        store.update_task(task_id, progress=50, release_info=f"{slot_key} 正在生成")
        reply = await server.chat(messages,
                                  max_tokens=config.slot_max_tokens(slot_key),
                                  temperature=0.3)
        elapsed = round((time.time() - t0) * 1000, 1)
        timings = server.last_timings or {}
        prefill_ms = timings.get("prompt_ms")
        if prefill_ms is None:
            prefill_ms = elapsed
        speed = timings.get("predicted_per_second")
        speed_txt = f"，{speed:.1f} tok/s" if isinstance(speed, (int, float)) else ""
        # 结果原样递回；同时写入会话历史，下轮前台能引用这个结果
        release_text = (f"{slot_key} 执行完成，总耗时 {elapsed}ms"
                        f"{speed_txt}{degrade_note}"
                        f"{'；输入已截断' if truncated else ''}")
        store.update_task(task_id, status="completed", progress=100,
                          final_reply=reply, prefill_ms=prefill_ms,
                          finish_reason=server.last_finish_reason or "stop",
                          release_info=release_text)
        # 结果已交付；历史补写失败只记录，绝不把 completed 回退成 failed
        try:
            store.append_message(t["session_id"], "assistant",
                                 prompts.history_message(reply))
        except Exception as exc:
            store.update_task(
                task_id,
                release_info=release_text + f"；历史写入失败: {type(exc).__name__}")
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        store.update_task(task_id, status="failed", progress=0,
                          error_msg=err[:500],
                          release_info="后台任务失败")


async def worker():
    while True:
        task_id = await queue.get()
        try:
            await process_task(task_id)
        except Exception as exc:
            store.update_task(task_id, status="failed", error_msg=str(exc)[:500])
        finally:
            queue.task_done()


@asynccontextmanager
async def lifespan(app):
    global front, slot, embedding, queue, worker_task, front_last_restart
    store.init()
    embedding = EmbeddingProvider()
    # 显存门槛只约束本地前台；api 前台无显存占用，探测失败也不阻断启动
    if (config.FRONT_TYPE == "local"
            and not config.VRAM_PLAN.get(config.FRONT_KEY, {}).get("available", True)):
        raise RuntimeError(
            f"前台槽位 {config.FRONT_KEY} 显存不足: "
            f"{config.VRAM_PLAN[config.FRONT_KEY]['reason']}")
    fm = config.MODELS_META[config.FRONT_KEY]
    front = create_backend(config.FRONT_KEY, fm, config.FRONT_PORT)
    await front.start()
    slot = SlotManager()
    queue = asyncio.Queue(maxsize=max(0, config.QUEUE_MAXSIZE))
    worker_task = asyncio.create_task(worker())
    # 重启恢复：pending 重新排队；processing 已被标记失败，避免永久卡住
    recovered = store.reset_inflight()
    for tid in recovered:
        try:
            queue.put_nowait(tid)
        except asyncio.QueueFull:
            store.update_task(tid, status="failed", error_msg="重启恢复时队列已满")
    if recovered:
        print(f"[startup] recovered {len(recovered)} pending task(s)", flush=True)
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await slot.stop()
    await front.stop()


app = FastAPI(title="explore 智能本地路由", version="2.5.0", lifespan=lifespan)
STARTED_AT = int(time.time())


class ChatRequest(BaseModel):
    user_input: str
    session_id: str | None = None
    prefer_tier: str | None = None


def _base_resp(text, session_id, task_id, tier, mode, instant, reason, status,
               base_url, queue_position=None, context=None, final_text=None,
               finish_reason=None):
    return {
        "code": 200,
        "user_input": text,
        "session_id": session_id,
        "task_id": task_id,
        "task_type": tier,
        "process_mode": mode,
        "instant_reply": instant,
        # 与 /task_result 的 final_reply 对齐：只放干净结果，不含进度附注
        "最终回复": final_text if final_text is not None else "",
        "task_status": status,
        # stop = 正常结束；length = 撞到 max_tokens，内容被截断
        "finish_reason": finish_reason,
        "status": "success",
        "num_ctx": config.FRONT_NUM_CTX,
        "selection_reason": reason,
        "queue_position": queue_position,
        "context_usage": context or {},
        "remark": "前台即时回应；后台结果经 /task_result 原样递回",
        "query_path": f"/task_result?task_id={task_id}",
        "query_url": f"{base_url}/task_result?task_id={task_id}",
    }


def _explore_meta(result):
    """两条 OpenAI 入口共用的扩展字段，避免两处拼装漂移。"""
    meta = {
        "task_id": result["task_id"],
        "task_status": result["status"],
        "process_mode": result["mode"],
        "tier": result["tier"],
        "chosen_slot": result["slot_key"],
        "instant_reply": result["instant"],
        "queue_position": result["queue_position"],
        "degraded": result["degraded"],
        "history_truncated": result["history_truncated"],
        "history_messages": result["context_usage"].get("history_messages", 0),
        "context_compressed": result["context_usage"].get("context_compressed", False),
        "finish_reason": result.get("finish_reason"),
        "num_ctx": config.FRONT_NUM_CTX,
        "selection_reason": result["reason"],
    }
    if result["timed_out"]:
        meta["fallback"] = "timeout"
        meta["hint"] = "后台仍在运行，可轮询 /task_result?task_id=... 获取完整结果"
    return meta


async def _compact(session_id, dropped):
    """滚动压缩：已有摘要 + 新丢弃的轮次 -> 新摘要。

    不能每次对全量丢弃内容重算——摘要容量固定，新内容会挤掉最早的
    暗号与约束。这里按 `covered_messages` 只合并新增部分，并要求旧摘要
    中的事实原样保留。同会话并发请求经 per-session 锁串行化，
    避免重复摘要与 covered_messages 回退。

    返回 `(summary, cached, took_ms)`；失败时 summary 为空串，主链路
    照常按原逻辑丢弃——压缩是增强，不是新的失败点。
    """
    lock = _compact_locks.get(session_id)
    if lock is None:
        lock = _compact_locks[session_id] = asyncio.Lock()
    async with lock:
        return await _compact_locked(session_id, dropped)


async def _compact_locked(session_id, dropped):
    limit = config.COMPACTION_MAX_SUMMARY_CHARS
    prev = store.get_session_summary(session_id)
    covered = int(prev["covered_messages"]) if prev else 0

    if prev and len(dropped) == covered:
        return prev["summary"], True, 0
    if prev and len(dropped) > covered:
        new_part = dropped[covered:]
    else:
        new_part = dropped          # 历史结构变化（清理/重排）时从头压缩

    if not new_part:
        return (prev["summary"] if prev else ""), True, 0
    if not await ensure_front():
        return "", False, 0

    t0 = time.time()
    try:
        if prev and prev["summary"]:
            prompt = compaction.merge_prompt(prev["summary"], new_part, limit)
        else:
            prompt = compaction.build_prompt(new_part, limit)
        # 摘要调用同样持 front_chat_lock：与其它请求的即时回复串行，
        # 避免前台实例的 last_finish_reason / last_timings 被并发覆写
        async with front_chat_lock:
            summary = await front.chat(
                [{"role": "system", "content": compaction.SUMMARIZE_SYSTEM},
                 {"role": "user", "content": prompt}],
                max_tokens=min(1024, int(limit * 1.5)),
                temperature=0.1)
    except Exception as exc:
        print(f"[compaction] failed: {type(exc).__name__}: {exc}", flush=True)
        return "", False, 0
    summary = (summary or "").strip()
    if not summary:
        return "", False, 0
    if len(summary) > limit:
        summary = summary[: limit - 1] + "…"
    store.save_session_summary(session_id, summary, len(dropped))
    return summary, False, round((time.time() - t0) * 1000)


async def _wait_task(task_id, timeout_s):
    """轮询后台任务直到完成/失败或超时；返回 (task, timed_out)。"""
    deadline = time.time() + max(0.0, float(timeout_s))
    while time.time() < deadline:
        task = store.get_task(task_id)
        if task and task["status"] in ("completed", "failed"):
            return task, False
        await asyncio.sleep(0.2)
    return store.get_task(task_id), True


async def _pipeline(text, session_id, prefer_tier, base_url,
                    history_override=None, system_extra="", wait_timeout_s=None):
    """共享编排：分级、上下文预算、前台即时回复、direct/async 分流。

    `/chat` 与 `/v1/chat/completions` 都走这里，保证两条入口的路由决策、
    历史预算与降级规则完全一致。本函数只返回结构化结果，不构造 HTTP 响应。

    - `history_override`：请求自带历史（OpenAI 客户端），为 None 时读 SQLite；
    - `system_extra`：请求里的 system 消息，追加到前台 system 之后；
    - `wait_timeout_s`：走后台时等待完成的上限；None/0 表示不等待。
    """
    if not text:
        raise HTTPException(status_code=400, detail="user_input 不能为空")
    if len(text) > config.MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"user_input 过长：{len(text)} 字符，当前配置上限 {config.MAX_INPUT_CHARS}")
    if prefer_tier and prefer_tier not in config.TIER_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"prefer_tier 必须是 {config.TIER_ORDER} 之一，收到 {prefer_tier!r}")

    tier, reason = classify(text, prefer_tier)
    target_slot = config.TIER_TO_SLOT.get(tier) or config.DEFAULT_SLOT
    slot_key = slot_for_tier(tier)
    needs_backend = slot_key != config.FRONT_KEY
    degraded = slot_key != target_slot
    if degraded:
        reason += f"；{target_slot} 显存不足，已降级到 {slot_key}"

    # 队列已满时提前拒绝，不让用户白等一次前台生成
    if needs_backend and queue.full():
        raise HTTPException(
            status_code=429,
            detail=f"后台队列已满（上限 {config.QUEUE_MAXSIZE}），请稍后重试")

    # 自动上下文预算：短输入多带历史，长输入自动收紧，绝不越过前台窗口
    # （local 前台 = NUM_CTX；api 前台 = 该槽位 context_window）
    _available = max(0, config.front_ctx_available())
    _remaining = max(0, _available - len(text))
    history_budget = min(
        _remaining,
        max(config.HISTORY_MIN_CHARS,
            int(_remaining * config.HISTORY_BUDGET_RATIO)))
    dropped = []
    if history_override is None:
        history, dropped, history_truncated = store.get_history_split(
            session_id, max_chars=history_budget,
            max_messages=config.HISTORY_MAX_MESSAGES)
    else:
        # 请求自带历史（OpenAI 语义）：用同一套预算裁剪，不读库、不重复累积
        history, dropped, history_truncated = store.split_history(
            history_override, max_chars=history_budget)
    history_chars = sum(len(m["content"]) for m in history)

    # 先压缩再丢弃：把会被裁掉的早期轮次摘要进 system，保住事实与约束。
    # 摘要失败或内容太短时退回原行为，不阻塞主链路。
    summary, compaction_meta = "", {}
    dropped_chars = sum(len(m["content"]) for m in dropped)
    if (dropped and config.COMPACTION_ENABLED
            and dropped_chars >= config.COMPACTION_MIN_DROPPED_CHARS):
        summary, cached, took_ms = await _compact(session_id, dropped)
        if summary:
            compaction_meta = {
                "cached": cached,
                "ms": took_ms,
                "summary_chars": len(summary),
                "dropped_messages": len(dropped),
                "dropped_chars": dropped_chars,
            }

    active = store.active_tasks(session_id)

    system = config.FRONT_SYSTEM
    if system_extra:
        system += "\n\n" + system_extra.strip()
    if summary:
        system += compaction.note(summary)
    if history_truncated:
        system += prompts.HISTORY_TRUNCATED_NOTE
    if active:
        desc = "; ".join(
            f"{a['task_id'][:8]} {a['status']} {a['progress']}%" for a in active)
        system += f"\n当前后台任务：{desc}。用户若问进度，据实简要说明，不要编造结果。"

    context_usage = {
        "num_ctx": config.FRONT_NUM_CTX,
        "input_chars": len(text),
        "history_chars": history_chars,
        "history_messages": len(history),
        "history_budget_chars": history_budget,
        "history_truncated": history_truncated,
        "context_compressed": bool(compaction_meta),
        "summary_chars": compaction_meta.get("summary_chars", 0),
        "compaction": compaction_meta,
        "front_max_tokens": config.FRONT_MAX_TOKENS,
        # 用实际 system 长度（含摘要）估算，避免压缩后仍按旧长度算
        "estimated_total_chars": (len(text) + history_chars
                                  + config.FRONT_MAX_TOKENS
                                  + len(system)
                                  + config.CONTEXT_SAFETY_TOKENS),
    }

    # 进度提问时才附真实状态；弱进度词要求确实有进行中的任务，避免误伤正常提问
    lowered = text.lower()
    wants_progress = any(w in lowered for w in STRONG_PROGRESS_WORDS) or (
        bool(active) and any(w in lowered for w in WEAK_PROGRESS_WORDS))
    progress_note = ""
    if wants_progress:
        recent = store.recent_tasks(session_id, limit=3,
                                    within_s=config.PROGRESS_WINDOW_S)
        if recent:
            rdesc = "; ".join(
                f"{t['task_id'][:8]} {t['status']} {t['progress']}%" for t in recent)
            progress_note = prompts.progress_note(rdesc)

    messages = [{"role": "system", "content": system}] + history + [
        {"role": "user", "content": text}]

    clean_instant = ""
    front_finish = "stop"
    try:
        # 前台实例会被即时回复与压缩摘要并发调用；last_finish_reason /
        # last_timings 是实例级共享属性，串行化避免并发交叉覆写
        async with front_chat_lock:
            if not await ensure_front():
                raise RuntimeError("前台模型不可用且处于重启冷却期")
            clean_instant = await front.chat(
                messages, max_tokens=config.FRONT_MAX_TOKENS, temperature=0.4)
            # 前台同样会撞 max_tokens；停止原因必须透传，否则半句话会被当成完整回答
            front_finish = front.last_finish_reason or "stop"
    except Exception as exc:
        clean_instant = prompts.front_failure(exc)
    instant = clean_instant.rstrip() + progress_note if progress_note else clean_instant

    # 历史只存干净回复，避免把进度附言带进后续上下文
    store.append_message(session_id, "user", text)
    store.append_message(session_id, "assistant", clean_instant)

    task_id = str(uuid.uuid4())
    if not needs_backend:
        store.create_task(task_id, session_id, text, tier, clean_instant, reason,
                      num_ctx=config.FRONT_NUM_CTX)
        store.update_task(task_id, status="completed", progress=100,
                          final_reply=clean_instant, finish_reason=front_finish,
                          release_info="简单任务由前台槽位直接完成")
        return {
            "text": text, "session_id": session_id, "task_id": task_id,
            "tier": tier, "reason": reason, "mode": "direct",
            "instant": instant, "final": clean_instant, "status": "completed",
            "queue_position": None, "context_usage": context_usage,
            "degraded": degraded, "slot_key": slot_key,
            "history_chars": history_chars, "history_truncated": history_truncated,
            "timed_out": False, "error": "", "finish_reason": front_finish,
        }

    store.create_task(task_id, session_id, text, tier, clean_instant, reason,
                      num_ctx=config.FRONT_NUM_CTX)
    try:
        queue.put_nowait(task_id)
    except asyncio.QueueFull:
        store.update_task(task_id, status="failed", error_msg="后台队列已满")
        raise HTTPException(status_code=429, detail="后台队列已满，请稍后重试")
    queue_position = max(0, queue.qsize() - 1)

    final, status, timed_out, error = "", "pending", False, ""
    finish_reason = None
    if wait_timeout_s:
        task, timed_out = await _wait_task(task_id, wait_timeout_s)
        if task:
            status = task["status"]
            final = task["final_reply"] or ""
            error = task["error_msg"] or ""
            finish_reason = task["finish_reason"]
    return {
        "text": text, "session_id": session_id, "task_id": task_id,
        "tier": tier, "reason": reason, "mode": "async",
        "instant": instant, "final": final, "status": status,
        "queue_position": queue_position, "context_usage": context_usage,
        "degraded": degraded, "slot_key": slot_key,
        "history_chars": history_chars, "history_truncated": history_truncated,
        "timed_out": timed_out, "error": error,
        "finish_reason": finish_reason or (front_finish if final else None),
    }


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    text = (req.user_input or "").strip()
    base_url = str(request.base_url).rstrip("/")
    result = await _pipeline(text, req.session_id or str(uuid.uuid4()),
                             req.prefer_tier, base_url)
    return JSONResponse(_base_resp(
        result["text"], result["session_id"], result["task_id"], result["tier"],
        result["mode"], result["instant"], result["reason"], result["status"],
        base_url, queue_position=result["queue_position"],
        context=result["context_usage"], final_text=result["final"],
        finish_reason=result.get("finish_reason")))


@app.get("/task_result")
async def task_result(task_id: str = Query(...)):
    t = store.get_task(task_id)
    if not t:
        return JSONResponse({"code": 404, "task_id": task_id,
                             "status": "not_found", "message": "任务不存在"})
    return JSONResponse({
        "code": 200,
        "task_id": task_id,
        "session_id": t["session_id"],
        "task_status": t["status"],
        "progress": f"{t['progress']}%",
        "tier": t["tier"],
        "chosen_model": t["model_name"],
        "instant_reply": t["instant_reply"] or "",
        "final_reply": t["final_reply"] or "",
        "最终回复": t["final_reply"] or "",
        "release_info": t["release_info"] or "",
        "error_msg": t["error_msg"] or "",
        "prefill_ms": t["prefill_ms"],
        "num_ctx": t["num_ctx"],
        "finish_reason": t["finish_reason"],
        "status": "success",
        "message": {
            "pending": "任务排队中，等待后台槽位",
            "processing": "任务处理中",
            "completed": "任务完成，结果原样递回",
            "failed": f"任务失败：{t['error_msg'] or '未知错误'}",
        }.get(t["status"], "未知状态"),
    })


@app.get("/health")
async def health():
    snap = await asyncio.to_thread(gpu.snapshot, config.GPU_BACKEND_PREF)
    return JSONResponse({
        "code": 200,
        "status": "success",
        "config": config.describe(),
        # 顶层保留兼容字段，旧客户端与测试脚本可直接读
        "backend": config.GPU_BACKEND,
        "num_ctx": config.NUM_CTX,
        "started_at": STARTED_AT,
        "uptime_s": max(0, int(time.time()) - STARTED_AT),
        "frontend": "ready" if (front and await front.healthy()) else "down",
        "frontend_load_ms": front.load_ms if front else None,
        "slot": slot.describe() if slot else {},
        "queue_depth": queue.qsize() if queue else 0,
        "embedding": embedding.describe() if embedding else {},
        "vram": {"total_mb": snap["total_mb"], "used_mb": snap["used_mb"],
                 "free_mb": snap["free_mb"]},
        "gpus": snap["gpus"],
    })


@app.get("/gpu")
async def gpu_info():
    return JSONResponse(await asyncio.to_thread(gpu.snapshot, config.GPU_BACKEND_PREF))


@app.get("/config")
async def config_info():
    """当前生效配置，换模型/换设备后用它确认解析结果。"""
    return JSONResponse(config.describe())


@app.get("/tasks")
async def tasks(limit: int = Query(20, ge=1, le=200)):
    return JSONResponse({"code": 200, "tasks": store.list_tasks(limit)})


@app.get("/console")
async def console():
    """内置 Web 控制台（单文件页面，无外部依赖）。"""
    return HTMLResponse(console_page())


def _require_admin(request: Request, write: bool = False):
    """管理接口守卫。

    - 设置了 ADMIN_TOKEN：写操作校验 X-Admin-Token 请求头（不接受 query 参数，
      避免令牌落访问日志）；
    - 未设置令牌：沿用 127.0.0.1 信任边界，但写操作额外校验 Origin/Host 与
      Content-Type，防跨站 CSRF（恶意网页可用 no-cors 表单 POST 打本机端口）。
    """
    if write:
        origin = request.headers.get("Origin")
        host = (request.headers.get("Host") or "").split(":")[0]
        if origin:
            o_host = origin.split("://")[1].split(":")[0] if "://" in origin else ""
            if o_host != host:
                raise HTTPException(status_code=403,
                                    detail="Origin 与服务地址不符，已拒绝跨站写操作")
        if host and host not in ("127.0.0.1", "localhost", "::1"):
            raise HTTPException(status_code=403,
                                detail=f"Host 头异常（{host}），已拒绝写操作")
        if request.method == "POST":
            ctype = (request.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                raise HTTPException(status_code=415,
                                    detail="Content-Type 必须是 application/json")
    if not ADMIN_TOKEN:
        return
    token = request.headers.get("X-Admin-Token") or ""
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401,
                            detail="admin token 无效（请求头 X-Admin-Token）")


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v)


@app.get("/admin/config")
async def admin_config(request: Request):
    """生效配置 + 可热调参数元数据（当前值 / 基础值 / 分组 / 说明）。"""
    _require_admin(request)
    data = config.describe()
    data["admin_token_required"] = bool(ADMIN_TOKEN)
    data["hot_params"] = {
        k: {
            "label": spec["label"], "group": spec["group"],
            "yaml_path": list(spec["yaml"]),
            "current": _jsonable(getattr(config, k)),
            "base": _jsonable(config.HOT_PARAM_BASE[k]),
            "changed": _jsonable(getattr(config, k)) != _jsonable(config.HOT_PARAM_BASE[k]),
        }
        for k, spec in config.HOT_PARAMS.items()
    }
    return JSONResponse(data)


@app.post("/admin/config")
async def admin_config_set(request: Request):
    """热调参数：{参数名: 值}。立即生效并持久化到 settings.local.yaml。"""
    _require_admin(request, write=True)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 {参数名: 值} 映射")
    applied, err = config.apply_runtime_params(body)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return JSONResponse({"code": 200, "status": "success", "applied": applied,
                         "note": "已写入 settings.local.yaml，重启后仍生效"})


@app.delete("/admin/config/{name}")
async def admin_config_reset(name: str, request: Request):
    """恢复参数为 import 时的基础值，并从覆盖层移除。"""
    _require_admin(request, write=True)
    value, err = config.reset_runtime_param(name)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return JSONResponse({"code": 200, "status": "success", "reset": name,
                         "value": _jsonable(value)})


# ---- 配置管理：启动期参数（槽位/模型/端口等）与重启 -------------------------
# 修改写入 settings.local.yaml 覆盖层（settings.yaml 注释不受影响），
# 保存时用真实子进程导入做预检，重启后生效。热调参数走 /admin/config。

_RESTART_CHILD = '''
import json, os, socket, subprocess, sys, time
spec = json.loads(sys.argv[1])
time.sleep(1.0)
env = os.environ.copy()
env["PYTHONPATH"] = spec["pythonpath"] + os.pathsep + env.get("PYTHONPATH", "")
# 端口以「候选配置实际解析值」为准：覆盖层可能改过 runtime.ports.api
port = spec["port"]
try:
    sys.path.insert(0, spec["pylib"])
    sys.path.insert(0, spec["root"])
    from app import config as _cfg
    port = int(_cfg.API_PORT)
except Exception as _e:
    print(f"[restart] resolve API_PORT failed, fallback {port}: {_e}", flush=True)
argv = list(spec["argv"])
if "--port" in argv:
    argv[argv.index("--port") + 1] = str(port)
# 等旧实例退出（API 端口释放）再拉新实例：优雅关闭可能远超固定秒数
# （SSE 排空最长 wait_timeout_s + 模型进程退出），固定 sleep 会抢端口失败
deadline = time.time() + 90
while time.time() < deadline:
    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            break
    time.sleep(1.0)
lf = open(spec["log"], "ab")
kw = ({"start_new_session": True} if os.name == "posix"
      else {"creationflags": 0x00000008 | 0x00000200})
p = subprocess.Popen(argv, stdout=lf, stderr=subprocess.STDOUT,
                     cwd=spec["cwd"], env=env, **kw)
with open(spec["pid_file"], "w") as f:
    f.write(str(p.pid))
print(f"[restart] new instance pid={p.pid} port={port}", flush=True)
'''


def _restart_child_spec():
    pid_file = os.getenv("PID_FILE", str(config.DATA / "router.pid"))
    log_file = os.getenv("LOG", str(config.LOGS / "api.log"))
    return {
        "argv": [sys.executable, "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", str(config.API_PORT),
                 "--workers", "1"],
        "port": int(config.API_PORT),
        "pythonpath": os.pathsep.join([str(config.ROOT), str(config.ROOT / "pylib")]),
        "root": str(config.ROOT),
        "cwd": str(config.ROOT),
        "log": log_file,
        "pid_file": pid_file,
    }


@app.get("/admin/settings")
async def admin_settings(request: Request):
    """全量启动期配置编辑视图：字段 + 中文注释 + 当前值 + 来源。"""
    _require_admin(request)
    return JSONResponse(config.settings_schema())


@app.post("/admin/settings")
async def admin_settings_set(request: Request):
    """写入/移除/删除启动期配置（覆盖层），预检通过后落盘，重启生效。

    body: {"updates": {path: value}, "resets": [path], "removes": [path]}
    - updates：写覆盖值（如 slots.cloud.base_url）；api_key 的掩码值 "***" 表示未修改；
    - resets：移除覆盖值，恢复 settings.yaml 原值；
    - removes：彻底删除（如删除槽位 slots.moe，settings.yaml 来源同样删掉）。
    """
    _require_admin(request, write=True)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是映射")
    updates = body.get("updates") or {}
    resets = body.get("resets") or []
    removes = body.get("removes") or []
    if not isinstance(updates, dict) or not isinstance(resets, list) \
            or not isinstance(removes, list):
        raise HTTPException(
            status_code=400,
            detail="格式应为 {updates: {path: value}, resets: [path], removes: [path]}")
    # api_key 只出掩码；掩码值 = 未修改，直接丢弃，避免把 "***" 写成真密钥
    updates = {k: v for k, v in updates.items()
               if not (k.endswith(".api_key") and v == "***")}
    if not updates and not resets and not removes:
        raise HTTPException(status_code=400, detail="没有任何变更")
    candidate, err = config.build_candidate_overlay(updates, resets, removes)
    if err:
        raise HTTPException(status_code=400, detail=err)
    # 子进程导入耗时数秒：放线程池执行，避免阻塞事件循环冻结在途请求
    ok, verr = await asyncio.to_thread(config.validate_overlay, candidate)
    if not ok:
        raise HTTPException(status_code=400,
                            detail=f"配置预检失败（未保存）: {verr}")
    config.commit_overlay(candidate)
    return JSONResponse({
        "code": 200, "status": "saved",
        "applied": {"updates": sorted(updates), "resets": resets,
                    "removes": removes},
        "note": "已写入 settings.local.yaml 并通过启动预检；重启服务后生效",
    })


@app.post("/admin/restart")
async def admin_restart(request: Request):
    """重启服务：先做配置预检（起不来就拒绝重启），再优雅退出并由
    守护子进程用相同参数拉起新实例。子进程会等旧实例退出（API 端口释放）
    后才拉起新实例；前端轮询 /health（started_at 变化）等待恢复。"""
    global RESTARTING
    _require_admin(request, write=True)
    if RESTARTING:
        raise HTTPException(status_code=409,
                            detail="重启已在进行中，请等待服务恢复后再操作")
    ok, err = await asyncio.to_thread(
        config.validate_overlay, copy.deepcopy(config.OVERLAY_LOADED))
    if not ok:
        raise HTTPException(status_code=400,
                            detail=f"配置预检失败，已取消重启: {err}")
    RESTARTING = True
    subprocess.Popen(
        [sys.executable, "-c", _RESTART_CHILD, json.dumps(_restart_child_spec())],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(config.ROOT),
        start_new_session=(os.name == "posix"),
        creationflags=(0x00000008 | 0x00000200) if os.name != "posix" else 0)

    async def _shutdown_soon():
        await asyncio.sleep(0.8)
        print("[restart] graceful shutdown requested via /admin/restart",
              flush=True)
        if os.name == "posix":
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            signal.raise_signal(signal.SIGINT)

    asyncio.create_task(_shutdown_soon())
    return JSONResponse({
        "code": 200, "status": "restarting",
        "note": "配置预检通过，服务正在优雅重启（通常 3-15 秒），可轮询 /health 等待恢复",
    })


async def _stream_completion(parsed, result, req_id, created):
    """SSE：先发即时回复保证首字，后台完成后再补最终结果。

    每 10 秒发一次 SSE 注释行做心跳，避免长时间无数据被反向代理断开。
    后台失败时补一段提示文本，而不是静默截断。
    """
    model = parsed["model"]
    yield openai_api.sse(openai_api.chunk_payload(
        req_id, created, model, {"role": "assistant", "content": ""}))
    instant = result["instant"] or ""
    for piece in openai_api.split_text(instant):
        yield openai_api.sse(openai_api.chunk_payload(
            req_id, created, model, {"content": piece}))

    written = len(instant)
    task = None
    if result["mode"] == "async" and result["status"] in ("pending", "processing"):
        deadline = time.time() + config.OPENAI_WAIT_TIMEOUT_S
        ticks = 0
        while time.time() < deadline:
            task = store.get_task(result["task_id"])
            if task and task["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(1.0)
            ticks += 1
            if ticks % 10 == 0:
                yield ": keep-alive\n\n"
        if task and task["status"] == "completed":
            final = task["final_reply"] or ""
            # 两段之间留一个空行，避免即时回复与最终结果粘成一句
            if final and final != instant:
                for piece in openai_api.split_text("\n\n" + final):
                    yield openai_api.sse(openai_api.chunk_payload(
                        req_id, created, model, {"content": piece}))
                written += 2 + len(final)
        elif task and task["status"] == "failed":
            note = f"\n\n[后台任务失败] {task.get('error_msg') or '未知错误'}"
            for piece in openai_api.split_text(note):
                yield openai_api.sse(openai_api.chunk_payload(
                    req_id, created, model, {"content": piece}))
            written += len(note)

    finish = "stop"
    if task and task.get("finish_reason"):
        finish = task["finish_reason"]
    elif result.get("finish_reason"):
        finish = result["finish_reason"]
    if finish not in ("stop", "length"):
        finish = "stop"
    yield openai_api.sse(openai_api.chunk_payload(
        req_id, created, model, {}, finish_reason=finish))
    if parsed["include_usage"]:
        yield openai_api.sse(openai_api.chunk_payload(
            req_id, created, model,
            usage=openai_api.estimate_usage(
                len(result["text"]) + result["history_chars"], written)))
    yield openai_api.sse_done()


@app.get("/v1/models")
async def openai_models():
    """OpenAI 客户端的模型探测；返回的是路由别名，不是真实权重名。"""
    return JSONResponse(openai_api.models_payload(config.TIER_ORDER, STARTED_AT))


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    """OpenAI 兼容入口：标准请求进，标准响应或 SSE 出。

    与 `/chat` 的区别只在协议形态；路由、预算、降级、结果原样递回完全一致。
    请求内 messages 自带历史时服务端按无状态处理（不读 SQLite）；
    只发一条 user 且带 `X-Session-Id` 或 `user` 字段时，才读服务端历史。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            openai_api.error_payload("请求体不是合法 JSON"), status_code=400)
    try:
        parsed = openai_api.parse_request(body, dict(request.headers))
    except openai_api.OpenAIError as exc:
        return JSONResponse(
            openai_api.error_payload(exc.message, exc.type, exc.code, exc.param),
            status_code=exc.status)

    # 流式必须先吐即时回复，所以不在编排层等待；非流式 final 模式才等后台
    wait_s = 0 if parsed["stream"] else (
        config.OPENAI_WAIT_TIMEOUT_S if parsed["mode"] == openai_api.MODE_FINAL else 0)
    history_override = parsed["history"] if parsed["history"] else (
        None if parsed["session_id"] else [])
    session_id = parsed["session_id"] or f"oai-{uuid.uuid4().hex[:12]}"
    try:
        result = await _pipeline(
            parsed["text"], session_id, parsed["tier"],
            str(request.base_url).rstrip("/"),
            history_override=history_override,
            system_extra=parsed["system_extra"],
            wait_timeout_s=wait_s)
    except HTTPException as exc:
        type_ = "rate_limit_error" if exc.status_code == 429 else "invalid_request_error"
        code = "context_length_exceeded" if exc.status_code == 413 else None
        return JSONResponse(
            openai_api.error_payload(str(exc.detail), type_, code),
            status_code=exc.status_code)

    req_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    if parsed["stream"]:
        return StreamingResponse(
            _stream_completion(parsed, result, req_id, created),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if result["status"] == "failed":
        return JSONResponse(
            openai_api.error_payload(
                f"后台任务失败：{result['error'] or '未知错误'}",
                "server_error", "backend_failed"),
            status_code=502)

    if result["mode"] == "async" and result["status"] == "completed":
        content = result["final"] or result["instant"]
    else:
        content = result["instant"]

    finish = result.get("finish_reason") or "stop"
    if finish not in ("stop", "length", "content_filter"):
        finish = "stop"
    return JSONResponse(openai_api.completion_payload(
        req_id, created, parsed["model"], content, finish,
        openai_api.estimate_usage(
            len(result["text"]) + result["history_chars"], len(content)),
        _explore_meta(result)))


async def _stream_response(parsed, result, resp_id, msg_id, created):
    """Responses 流式：细分事件 + `event:` 行，**不以 [DONE] 结尾**。

    与 chat/completions 一样先吐即时回复；后台完成后再续写最终结果，
    客户端看到的仍是一条完整 message。每 10 秒发 SSE 注释行做心跳。
    """
    model = parsed["model"]
    seq = 0

    def frame(event_type, **fields):
        nonlocal seq
        body = {"type": event_type, "sequence_number": seq}
        body.update(fields)
        seq += 1
        return openai_api.response_event(event_type, body)

    def base_response(status, text, usage, output, incomplete_reason=None):
        return openai_api.response_payload(
            resp_id, created, model, text, status, usage, _explore_meta(result),
            msg_id,
            instructions=parsed["instructions"],
            max_output_tokens=parsed["requested_max_tokens"],
            temperature=parsed["requested_temperature"],
            previous_response_id=parsed["previous_response_id"],
            store=parsed["store"], output=output,
            incomplete_reason=incomplete_reason)

    created_resp = base_response("in_progress", "", None, [])
    yield frame("response.created", response=created_resp)
    yield frame("response.in_progress", response=created_resp)

    pending_item = {"type": "message", "id": msg_id, "status": "in_progress",
                    "role": "assistant", "content": []}
    yield frame("response.output_item.added", output_index=0, item=pending_item)
    yield frame("response.content_part.added", item_id=msg_id, output_index=0,
                content_index=0,
                part={"type": "output_text", "text": "", "annotations": []})

    instant = result["instant"] or ""
    for piece in openai_api.split_text(instant):
        yield frame("response.output_text.delta", item_id=msg_id,
                    output_index=0, content_index=0, delta=piece)
    written = len(instant)
    final_text = instant
    task = None

    if result["mode"] == "async" and result["status"] in ("pending", "processing"):
        deadline = time.time() + config.OPENAI_WAIT_TIMEOUT_S
        ticks = 0
        while time.time() < deadline:
            task = store.get_task(result["task_id"])
            if task and task["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(1.0)
            ticks += 1
            if ticks % 10 == 0:
                yield ": keep-alive\n\n"
        if task and task["status"] == "completed":
            extra = task.get("final_reply") or ""
            if extra and extra != instant:
                for piece in openai_api.split_text("\n\n" + extra):
                    yield frame("response.output_text.delta", item_id=msg_id,
                                output_index=0, content_index=0, delta=piece)
                written += 2 + len(extra)
                final_text = instant + "\n\n" + extra
        elif task and task["status"] == "failed":
            note = f"\n\n[后台任务失败] {task.get('error_msg') or '未知错误'}"
            for piece in openai_api.split_text(note):
                yield frame("response.output_text.delta", item_id=msg_id,
                            output_index=0, content_index=0, delta=piece)
            written += len(note)
            final_text = instant + note

    usage = openai_api.response_usage(
        len(result["text"]) + result["history_chars"], written)
    part = {"type": "output_text", "text": final_text, "annotations": []}
    full_item = openai_api.message_item(msg_id, final_text)
    yield frame("response.output_text.done", item_id=msg_id, output_index=0,
                content_index=0, text=final_text)
    yield frame("response.content_part.done", item_id=msg_id, output_index=0,
                content_index=0, part=part)
    yield frame("response.output_item.done", output_index=0, item=full_item)
    finish = "stop"
    if task and task.get("finish_reason"):
        finish = task["finish_reason"]
    elif result.get("finish_reason"):
        finish = result["finish_reason"]
    final_status = "incomplete" if finish == "length" else "completed"
    incomplete_reason = "max_output_tokens" if finish == "length" else None
    event = ("response.incomplete" if final_status == "incomplete"
             else "response.completed")
    yield frame(event, response=base_response(final_status, final_text, usage,
                                              [full_item], incomplete_reason))
    if parsed["store"]:
        store.save_response(resp_id, result["session_id"], result["task_id"],
                            model, result["text"], final_text, final_status)


@app.post("/v1/responses")
async def openai_responses(request: Request):
    """OpenAI Responses API 兼容入口。

    `previous_response_id` 续接服务端会话历史（需上一轮 `store` 为 true）；
    `input` 自带完整历史时按无状态处理。采样、路由、降级与 `/chat` 一致。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            openai_api.error_payload("请求体不是合法 JSON"), status_code=400)
    try:
        parsed = openai_api.parse_responses_request(body, dict(request.headers))
    except openai_api.OpenAIError as exc:
        return JSONResponse(
            openai_api.error_payload(exc.message, exc.type, exc.code, exc.param),
            status_code=exc.status)

    prev_session = None
    if parsed["previous_response_id"]:
        prev = store.get_response(parsed["previous_response_id"])
        if not prev:
            return JSONResponse(
                openai_api.error_payload(
                    f"previous_response_id 不存在：{parsed['previous_response_id']}",
                    "invalid_request_error", "previous_response_not_found",
                    "previous_response_id"),
                status_code=404)
        prev_session = prev["session_id"]

    if parsed["previous_response_id"]:
        history_override = None          # 续接服务端会话历史
    elif parsed["history"]:
        history_override = parsed["history"]
    elif parsed["session_id"]:
        history_override = None          # 单条 input + 显式会话 id -> 读库
    else:
        history_override = []

    session_id = (parsed["session_id"] or prev_session
                  or f"resp-{uuid.uuid4().hex[:12]}")
    wait_s = 0 if parsed["stream"] else (
        config.OPENAI_WAIT_TIMEOUT_S if parsed["mode"] == openai_api.MODE_FINAL else 0)
    try:
        result = await _pipeline(
            parsed["text"], session_id, parsed["tier"],
            str(request.base_url).rstrip("/"),
            history_override=history_override,
            system_extra=parsed["system_extra"],
            wait_timeout_s=wait_s)
    except HTTPException as exc:
        type_ = "rate_limit_error" if exc.status_code == 429 else "invalid_request_error"
        code = "context_length_exceeded" if exc.status_code == 413 else None
        return JSONResponse(
            openai_api.error_payload(str(exc.detail), type_, code),
            status_code=exc.status_code)

    resp_id = "resp_" + uuid.uuid4().hex[:24]
    msg_id = "msg_" + resp_id[len("resp_"):]
    created = int(time.time())

    if parsed["stream"]:
        return StreamingResponse(
            _stream_response(parsed, result, resp_id, msg_id, created),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if result["status"] == "failed":
        return JSONResponse(
            openai_api.error_payload(
                f"后台任务失败：{result['error'] or '未知错误'}",
                "server_error", "backend_failed"),
            status_code=502)

    if result["mode"] == "async" and result["status"] == "completed":
        content = result["final"] or result["instant"]
    else:
        content = result["instant"]

    status, incomplete = "completed", None
    if result["timed_out"]:
        # 后台仍在跑：不谎称完成，用 incomplete + timeout 表明可续查
        status, incomplete = "incomplete", "timeout"
    elif (result.get("finish_reason") == "length"):
        # 撞到 max_tokens：内容确实被截断，不能报 completed
        status, incomplete = "incomplete", "max_output_tokens"
    usage = openai_api.response_usage(
        len(result["text"]) + result["history_chars"], len(content))
    payload = openai_api.response_payload(
        resp_id, created, parsed["model"], content, status, usage,
        _explore_meta(result), msg_id,
        instructions=parsed["instructions"],
        max_output_tokens=parsed["requested_max_tokens"],
        temperature=parsed["requested_temperature"],
        previous_response_id=parsed["previous_response_id"],
        store=parsed["store"], incomplete_reason=incomplete)
    if parsed["store"]:
        store.save_response(resp_id, result["session_id"], result["task_id"],
                            parsed["model"], result["text"], content, status)
    return JSONResponse(payload)


@app.get("/v1/responses/{response_id}")
async def openai_get_response(response_id: str):
    """按 id 取回已存储的 response（store=true 时才有）。"""
    record = store.get_response(response_id)
    if not record:
        return JSONResponse(
            openai_api.error_payload(
                f"response 不存在：{response_id}", "invalid_request_error",
                "not_found"),
            status_code=404)
    text = record.get("output_text") or ""
    usage = openai_api.response_usage(
        len(record.get("input_text") or ""), len(text))
    msg_id = ("msg_" + response_id[len("resp_"):]
              if response_id.startswith("resp_") else "msg_" + response_id)
    return JSONResponse(openai_api.response_payload(
        response_id, int(record.get("created_at") or 0),
        record.get("model") or "", text, record.get("status") or "completed",
        usage,
        {"task_id": record.get("task_id"), "process_mode": "stored",
         "session_id": record.get("session_id")},
        msg_id))

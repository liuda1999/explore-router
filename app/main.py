"""explore 智能本地路由（配置驱动版）。

前台：由 settings.yaml 中 resident=true 的槽位常驻，句句有着落、即时回复、
      简单任务直接完成、进度应答；前台进程异常时自动重启一次。
后台：互斥槽位，默认常驻一个模型，需要另一档时才切换；结果原样递回。
配置：槽位、模型、硬件、上下文、端口、限额全部来自 settings.yaml。
状态：SQLite 持久化；重启时恢复未完成任务，processing 标记失败避免卡死。
"""
import asyncio
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import config, gpu, prompts, store
from .embed import EmbeddingProvider
from .llama import LlamaServer
from .router import SlotManager, classify, slot_for_tier

# 强进度词：明确在问任务状态；弱进度词：需要同时存在进行中的任务才算进度提问
STRONG_PROGRESS_WORDS = ("进度", "到哪了", "到哪一步", "完成了吗", "任务状态",
                         "处理完", "还在吗", "status")
WEAK_PROGRESS_WORDS = ("好了吗", "怎么样了", "怎么样")

front: LlamaServer | None = None
slot: SlotManager | None = None
embedding: EmbeddingProvider | None = None
queue: asyncio.Queue | None = None
worker_task = None
front_lock = asyncio.Lock()
front_last_restart = 0.0


async def ensure_front():
    """前台是常驻交互入口：不健康时重启一次，冷却期内不反复重启。"""
    global front_last_restart
    if front is None:
        return False
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
        # 超长输入按上下文预算截断中间部分，避免后台实例 prefill 溢出
        task_text = t["user_input"]
        task_limit = max(512, config.NUM_CTX - config.MAX_OUTPUT_TOKENS
                         - config.CONTEXT_SAFETY_TOKENS)
        truncated = len(task_text) > task_limit
        if truncated:
            head = task_limit * 2 // 3
            tail = task_limit - head
            task_text = (task_text[:head] + prompts.TRUNCATION_MARK
                         + task_text[-tail:])
        # 统一模板：system + user 两条，字段顺序固定，见 app/prompts.py
        messages = prompts.backend_messages(
            task_text, embedding.split(task_text), truncated=truncated)
        store.update_task(task_id, progress=50, release_info=f"{slot_key} 正在生成")
        reply = await server.chat(messages, max_tokens=config.MAX_OUTPUT_TOKENS,
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
    if not config.VRAM_PLAN.get(config.FRONT_KEY, {}).get("available", True):
        raise RuntimeError(
            f"前台槽位 {config.FRONT_KEY} 显存不足: "
            f"{config.VRAM_PLAN[config.FRONT_KEY]['reason']}")
    fm = config.MODELS_META[config.FRONT_KEY]
    front = LlamaServer(config.FRONT_KEY, config.FRONT_PORT, config.FRONT_MODEL,
                        fm["devices"], split_ratio=fm["split_ratio"])
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


app = FastAPI(title="explore 智能本地路由", version="2.2.0", lifespan=lifespan)


class ChatRequest(BaseModel):
    user_input: str
    session_id: str | None = None
    prefer_tier: str | None = None


def _base_resp(text, session_id, task_id, tier, mode, instant, reason, status,
               base_url, queue_position=None, context=None, final_text=None):
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
        "status": "success",
        "num_ctx": config.NUM_CTX,
        "selection_reason": reason,
        "queue_position": queue_position,
        "context_usage": context or {},
        "remark": "前台即时回应；后台结果经 /task_result 原样递回",
        "query_path": f"/task_result?task_id={task_id}",
        "query_url": f"{base_url}/task_result?task_id={task_id}",
    }


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    text = (req.user_input or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="user_input 不能为空")
    if len(text) > config.MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"user_input 过长：{len(text)} 字符，当前配置上限 {config.MAX_INPUT_CHARS}")
    if req.prefer_tier and req.prefer_tier not in config.TIER_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"prefer_tier 必须是 {config.TIER_ORDER} 之一，收到 {req.prefer_tier!r}")

    session_id = req.session_id or str(uuid.uuid4())
    base_url = str(request.base_url).rstrip("/")
    tier, reason = classify(text, req.prefer_tier)
    target_slot = config.TIER_TO_SLOT.get(tier) or config.DEFAULT_SLOT
    slot_key = slot_for_tier(tier)
    needs_backend = slot_key != config.FRONT_KEY
    if slot_key != target_slot:
        reason += f"；{target_slot} 显存不足，已降级到 {slot_key}"

    # 队列已满时提前拒绝，不让用户白等一次前台生成
    if needs_backend and queue.full():
        raise HTTPException(
            status_code=429,
            detail=f"后台队列已满（上限 {config.QUEUE_MAXSIZE}），请稍后重试")

    # 自动上下文预算：短输入多带历史，长输入自动收紧，绝不越过 num_ctx
    _available = max(0, config.NUM_CTX - config.FRONT_MAX_TOKENS
                     - config.CONTEXT_SAFETY_TOKENS - len(config.FRONT_SYSTEM))
    _remaining = max(0, _available - len(text))
    history_budget = min(
        _remaining,
        max(config.HISTORY_MIN_CHARS,
            int(_remaining * config.HISTORY_BUDGET_RATIO)))
    history, history_truncated = store.get_history(
        session_id, max_chars=history_budget,
        max_messages=config.HISTORY_MAX_MESSAGES, with_meta=True)
    history_chars = sum(len(m["content"]) for m in history)
    context_usage = {
        "num_ctx": config.NUM_CTX,
        "input_chars": len(text),
        "history_chars": history_chars,
        "history_messages": len(history),
        "history_budget_chars": history_budget,
        "history_truncated": history_truncated,
        "front_max_tokens": config.FRONT_MAX_TOKENS,
        "estimated_total_chars": (len(text) + history_chars
                                  + config.FRONT_MAX_TOKENS
                                  + len(config.FRONT_SYSTEM)
                                  + config.CONTEXT_SAFETY_TOKENS),
    }
    active = store.active_tasks(session_id)

    system = config.FRONT_SYSTEM
    if history_truncated:
        system += prompts.HISTORY_TRUNCATED_NOTE
    if active:
        desc = "; ".join(
            f"{a['task_id'][:8]} {a['status']} {a['progress']}%" for a in active)
        system += f"\n当前后台任务：{desc}。用户若问进度，据实简要说明，不要编造结果。"

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
    try:
        if not await ensure_front():
            raise RuntimeError("前台模型不可用且处于重启冷却期")
        clean_instant = await front.chat(messages, max_tokens=config.FRONT_MAX_TOKENS,
                                         temperature=0.4)
    except Exception as exc:
        clean_instant = prompts.front_failure(exc)
    instant = clean_instant.rstrip() + progress_note if progress_note else clean_instant

    # 历史只存干净回复，避免把进度附言带进后续上下文
    store.append_message(session_id, "user", text)
    store.append_message(session_id, "assistant", clean_instant)

    task_id = str(uuid.uuid4())
    if not needs_backend:
        store.create_task(task_id, session_id, text, tier, clean_instant, reason)
        store.update_task(task_id, status="completed", progress=100,
                          final_reply=clean_instant,
                          release_info="简单任务由前台槽位直接完成")
        return JSONResponse(_base_resp(text, session_id, task_id, tier,
                                       "direct", instant, reason, "completed",
                                       base_url, context=context_usage,
                                       final_text=clean_instant))

    store.create_task(task_id, session_id, text, tier, clean_instant, reason)
    try:
        queue.put_nowait(task_id)
    except asyncio.QueueFull:
        store.update_task(task_id, status="failed", error_msg="后台队列已满")
        raise HTTPException(status_code=429, detail="后台队列已满，请稍后重试")
    return JSONResponse(_base_resp(text, session_id, task_id, tier,
                                   "async", instant, reason, "pending",
                                   base_url,
                                   queue_position=max(0, queue.qsize() - 1),
                                   context=context_usage))


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

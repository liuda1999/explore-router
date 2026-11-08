"""OpenAI 兼容协议层（`/v1/models` 与 `/v1/chat/completions`）。

本模块只做协议转换，不接触模型进程、不读数据库：
- `parse_request`：标准 OpenAI 请求 -> 当前输入 / 请求内历史 / 模式 / 档位
- `completion_payload` / `chunk_payload` / `sse`：标准响应与 SSE 帧
- `models_payload`：`/v1/models` 列表
- `error_payload`：OpenAI 风格错误体

三条约定（与 app/prompts.py 一致）：
1. `choices[].message.content` 永远是「干净结果」，进度附注只出现在
   `explore.instant_reply`，客户端不会看到两套正文；
2. 非流式默认等后台完成，超时回退即时回复并标 `explore.fallback=timeout`；
3. SSE 是**分块发送**而非逐 token：底层 llama-server 调用为 `stream=false`，
   分块只保证协议兼容，不改变首字延迟（前台生成完才有首帧）。
"""
import json

from . import config

MODEL_AUTO = "explore"
MODEL_FAST = "explore-fast"
MODE_FINAL = "final"
MODE_INSTANT = "instant"
CHUNK_SIZE = 24


class OpenAIError(Exception):
    """协议层校验失败；由端点转换成 OpenAI 错误体。"""

    def __init__(self, message, status=400, type_="invalid_request_error",
                 code=None, param=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.type = type_
        self.code = code
        self.param = param


def _content_text(content):
    """content 可能是字符串，也可能是 [{type: text|input_text|output_text, text}]。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in (None, "text", "input_text", "output_text"):
                    parts.append(item.get("text") or "")
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def resolve_tier(model):
    """model 名 -> 档位；无法识别时返回 None（按自动路由处理）。

    支持三种写法，全部不区分大小写：
    - `explore` / `explore-auto` / 空 -> 自动
    - 档位名（settings.yaml 的 routing.tier_order，如 mid / heavy）
    - 槽位名（如 front / mid / moe），反查 tier_to_slot
    """
    name = (model or "").strip().lower()
    if name.startswith("explore-"):
        name = name[len("explore-"):]
    for suffix in ("-fast", "-instant"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if not name or name == "auto":
        return None
    if name in config.TIER_ORDER:
        return name
    for tier, slot in config.TIER_TO_SLOT.items():
        if str(slot).lower() == name:
            return tier
    return None


def _resolve_mode(body, headers, model):
    """返回 final / instant。

    优先级：body.explore_mode > X-Explore-Mode 头 > model 名 -fast 后缀
    > settings.yaml 的 openai.default_mode。
    """
    explicit = body.get("explore_mode")
    if explicit is None:
        explicit = headers.get("x-explore-mode")
    if explicit:
        value = str(explicit).strip().lower()
        if value in (MODE_FINAL, MODE_INSTANT):
            return value
        raise OpenAIError(
            f"explore_mode 必须是 final 或 instant，收到 {explicit!r}",
            param="explore_mode", code="invalid_mode")
    name = (model or "").strip().lower()
    if name.endswith("-fast") or name.endswith("-instant"):
        return MODE_INSTANT
    return config.OPENAI_DEFAULT_MODE


def parse_request(body, headers=None):
    """把 OpenAI 请求体解析成编排层需要的字段。

    当前输入取**最后一条 user 消息**，它之前的 user/assistant 作为请求内历史；
    system 消息合并后追加到服务端前台 system 之后（不替换，保留路由人格）。

    `max_tokens` / `temperature` 只回显不生效：本地路由由服务端统一控制采样，
    见文档「已知限制」。
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if not isinstance(body, dict):
        raise OpenAIError("请求体必须是 JSON 对象")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise OpenAIError("messages 不能为空", param="messages")

    system_parts, turns = [], []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = _content_text(item.get("content"))
        if role == "system":
            if text.strip():
                system_parts.append(text.strip())
        elif role in ("user", "assistant"):
            turns.append((role, text))

    user_index = [i for i, (role, _) in enumerate(turns) if role == "user"]
    if not user_index:
        raise OpenAIError("messages 中没有 user 消息", param="messages")
    index = user_index[-1]
    text = turns[index][1]
    if not text.strip():
        raise OpenAIError("最后一条 user 消息内容为空", param="messages")

    model = str(body.get("model") or MODEL_AUTO).strip()
    mode = _resolve_mode(body, headers, model)
    session_id = headers.get("x-session-id") or body.get("user") or None

    return {
        "text": text,
        "history": [{"role": role, "content": content}
                    for role, content in turns[:index]],
        "system_extra": "\n\n".join(system_parts),
        "model": model,
        "tier": resolve_tier(model),
        "mode": mode,
        "stream": bool(body.get("stream")),
        "include_usage": bool((body.get("stream_options") or {}).get("include_usage")),
        "session_id": str(session_id) if session_id else None,
        "requested_max_tokens": body.get("max_tokens") or body.get("max_completion_tokens"),
        "requested_temperature": body.get("temperature"),
    }


def completion_payload(req_id, created, model, content, finish_reason, usage, explore):
    return {
        "id": req_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
        "usage": usage,
        "explore": explore,
    }


def chunk_payload(req_id, created, model, delta=None, finish_reason=None, usage=None):
    # usage 帧按 OpenAI 约定是 choices 为空数组
    choices = [] if usage is not None else [{
        "index": 0,
        "delta": delta or {},
        "finish_reason": finish_reason,
    }]
    frame = {
        "id": req_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": choices,
    }
    if usage is not None:
        frame["usage"] = usage
    return frame


def sse(frame):
    return "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"


def sse_done():
    return "data: [DONE]\n\n"


def split_text(text, size=CHUNK_SIZE):
    """按固定字符数切块；空串返回一个空块，保证至少有一帧正文。"""
    if not text:
        return [""]
    return [text[i:i + size] for i in range(0, len(text), size)]


def estimate_usage(prompt_chars, completion_chars):
    """字符数 -> token 估算（中文约 1 token / 1.5 字，中英混合取 1.6）。

    llama-server 的 timings 只覆盖单次生成，跨槽位/跨请求不可比，
    统一用估算值并在文档中标注，避免给出看似精确实则错误的数字。
    """
    def tokens(n):
        return max(1, round(n / 1.6))
    prompt, completion = tokens(prompt_chars), tokens(completion_chars)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def models_payload(tiers, created):
    """虚拟模型名：explore（自动）、explore-fast（只回即时回复）、explore-<tier>。"""
    ids = [MODEL_AUTO, MODEL_FAST] + [f"explore-{tier}" for tier in tiers]
    return {
        "object": "list",
        "data": [{"id": name, "object": "model", "created": created,
                  "owned_by": "explore"} for name in ids],
    }


def error_payload(message, type_="invalid_request_error", code=None, param=None):
    return {"error": {"message": message, "type": type_,
                      "param": param, "code": code}}


# ── Responses API（/v1/responses）────────────────────────────────
#
# 与 chat/completions 的差异：
# - `input` 取代 `messages`（可为字符串或消息数组），`instructions` 取代 system；
# - `output[]` 里的 `output_text` 取代 `choices[].message.content`；
# - `previous_response_id` 表示服务端续接，`store` 控制是否落库；
# - 流式是细分事件（response.created / output_text.delta / response.completed …），
#   每帧带 `event:` 行与 `sequence_number`，且**不以 [DONE] 结尾**。

def parse_responses_request(body, headers=None):
    """Responses 请求 -> 编排层字段，字段名与 parse_request 对齐。"""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if not isinstance(body, dict):
        raise OpenAIError("请求体必须是 JSON 对象")
    if body.get("input") is None:
        raise OpenAIError("input 不能为空", param="input")

    raw = body["input"]
    system_parts, turns = [], []
    if isinstance(raw, str):
        text = raw
        if not text.strip():
            raise OpenAIError("input 不能为空", param="input")
        history = []
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = item.get("role") or "user"
            content = _content_text(item.get("content"))
            if role in ("system", "developer"):
                if content.strip():
                    system_parts.append(content.strip())
            elif role in ("user", "assistant"):
                turns.append((role, content))
        indexes = [i for i, (role, _) in enumerate(turns) if role == "user"]
        if not indexes:
            raise OpenAIError("input 中没有 user 消息", param="input")
        index = indexes[-1]
        text = turns[index][1]
        if not text.strip():
            raise OpenAIError("最后一条 user 消息内容为空", param="input")
        history = [{"role": role, "content": content}
                   for role, content in turns[:index]]
    else:
        raise OpenAIError("input 必须是字符串或消息数组", param="input")

    instructions = body.get("instructions")
    extra = []
    if isinstance(instructions, str) and instructions.strip():
        extra.append(instructions.strip())
    extra.extend(system_parts)

    model = str(body.get("model") or MODEL_AUTO).strip()
    mode = _resolve_mode(body, headers, model)
    session_id = headers.get("x-session-id") or body.get("user") or None
    previous = body.get("previous_response_id")
    return {
        "text": text,
        "history": history,
        "system_extra": "\n\n".join(extra),
        "model": model,
        "tier": resolve_tier(model),
        "mode": mode,
        "stream": bool(body.get("stream")),
        "session_id": str(session_id) if session_id else None,
        "previous_response_id": str(previous) if previous else None,
        "store": bool(body.get("store", True)),
        "instructions": instructions if isinstance(instructions, str) else None,
        "requested_max_tokens": body.get("max_output_tokens"),
        "requested_temperature": body.get("temperature"),
    }


def message_item(msg_id, text, status="completed"):
    return {
        "type": "message",
        "id": msg_id,
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def response_usage(prompt_chars, completion_chars):
    usage = estimate_usage(prompt_chars, completion_chars)
    return {
        "input_tokens": usage["prompt_tokens"],
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": usage["completion_tokens"],
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": usage["total_tokens"],
    }


def response_payload(resp_id, created, model, text, status, usage, explore,
                     msg_id, instructions=None, max_output_tokens=None,
                     temperature=None, previous_response_id=None, store=True,
                     error=None, incomplete_reason=None, output=None):
    """构造完整 response 对象；流式事件里的 response 字段复用它。"""
    if output is None:
        output = [message_item(
            msg_id, text,
            status="completed" if status == "completed" else "in_progress")]
    return {
        "id": resp_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "error": error,
        "incomplete_details": ({"reason": incomplete_reason}
                               if incomplete_reason else None),
        "instructions": instructions,
        "max_output_tokens": max_output_tokens,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": previous_response_id,
        "reasoning": {"effort": None, "summary": None},
        "store": store,
        "temperature": temperature,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": usage,
        "user": None,
        "metadata": {},
        "explore": explore,
    }


def response_event(event_type, payload):
    """Responses 流式帧：比 chat/completions 多一行 `event:`，且无 [DONE]。"""
    return ("event: " + event_type + "\ndata: "
            + json.dumps(payload, ensure_ascii=False) + "\n\n")

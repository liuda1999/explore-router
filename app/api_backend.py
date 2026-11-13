"""线上 API 槽位后端（OpenAI 兼容）。

与 LlamaServer 保持同一调用契约（chat / healthy / start / stop + alive /
load_ms / last_timings / last_finish_reason / last_error），但**没有本地进程**：
chat 是对 base_url 的 /chat/completions HTTP 调用，不占本地显存，
不参与互斥槽位切换。密钥只在请求头使用，绝不写日志或 /config。

create_backend() 是槽位后端的唯一工厂：main.py（前台常驻）与 router.py
（后台互斥槽位）都经由它构建——type: local -> LlamaServer，type: api -> ApiBackend。
"""
import asyncio
import time

import httpx

from . import config
from .llama import LlamaServer


class ApiBackend:
    """线上模型的槽位后端（provider: openai | ollama）。

    - openai（默认）：OpenAI 兼容 /v1/chat/completions；
    - ollama：本机/远端 Ollama 原生 /api/chat——thinking 模型（qwen3.5 等）
      在 OpenAI 兼容端点会把全部输出放进 reasoning、content 为空，且兼容层
      忽略 think 参数，因此 ollama provider 默认携带 think:false（extra_body
      可覆盖），并把 /api/tags 作为健康探测端点。

    start() 只做一次连通性探测：探测失败仅记日志不抛错——云端临时不可用
    不应阻止服务启动，请求期已有 front_failure / 任务失败兜底。
    chat() 复用本地后端的重试语义：5xx / 429 / 连接类错误重试一次，
    绝不静默返回空串。
    """

    def __init__(self, name, base_url, model, api_key="", context_window=None,
                 provider="openai", extra_body=None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.provider = str(provider or "openai").lower()
        self.extra_body = dict(extra_body or {})
        self.context_window = int(context_window) if context_window else None
        self.proc = None                 # 无本地进程，占位以兼容健康检查代码
        self.started_at = None
        self.load_ms = None
        self.last_error = ""
        self.last_timings = {}
        self.last_finish_reason = None
        self._client = None

    @property
    def base(self):
        return self.base_url

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _log_line(self, text):
        try:
            with open(config.LOGS / f"{self.name}.log", "a", encoding="utf-8") as f:
                f.write(text.rstrip("\n") + "\n")
        except Exception:
            pass

    def alive(self):
        return True

    async def _ensure_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT_S)
        return self._client

    async def start(self):
        """启动期探测一次；不可达只写日志，不阻断启动。"""
        self.started_at = time.time()
        ok, detail = await self._ping()
        self.load_ms = 0.0
        if ok and "404" not in detail:
            self._log_line(f"[api] start ok: {self.base_url} model={self.model}")
        else:
            level = "degraded" if ok else "failed"
            self._log_line(f"[api] start probe {level} (service keeps running): "
                           f"{self.base_url} -> {detail}")
            print(f"[warn] api slot {self.name} probe {level}: {detail}",
                  flush=True)

    async def _ping(self):
        """连通性探测，返回 (healthy, detail)。

        - ollama：GET /api/tags（模型清单端点，本机部署通常恒 200）；
        - openai：GET /models——2xx 健康；401/403 鉴权失败判不健康；
          404 判健康但提示可能缺 /v1；5xx/网络错误不健康。
        """
        probe_path = "/api/tags" if self.provider == "ollama" else "/models"
        try:
            c = await self._ensure_client()
            r = await c.get(self.base_url + probe_path, headers=self._headers(),
                            timeout=5)
            if 200 <= r.status_code < 300:
                return True, f"{probe_path}={r.status_code}"
            if r.status_code in (401, 403):
                return False, (f"{probe_path}={r.status_code}（鉴权失败：检查 "
                               f"api_key_env 指定的环境变量是否已设置/正确）")
            if r.status_code == 404 and self.provider == "openai":
                return True, (f"{probe_path}=404（端点不存在：确认 base_url 是否"
                              f"缺少版本段如 /v1，或厂商不提供 /models）")
            return r.status_code < 500, f"{probe_path}={r.status_code}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {str(exc)[:120]}"

    async def healthy(self):
        ok, _ = await self._ping()
        return ok

    def _chat_request(self, messages, max_tokens, temperature):
        """按 provider 构造 (path, payload)。"""
        if self.provider == "ollama":
            payload = {"model": self.model, "messages": messages,
                       "stream": False, "temperature": temperature,
                       # thinking 模型默认关思考：OpenAI 兼容层会把全部输出
                       # 放进 reasoning、content 恒空；extra_body 可覆盖
                       "think": False,
                       "options": {"num_predict": max_tokens}}
            payload.update(self.extra_body)
            return "/api/chat", payload
        payload = {"model": self.model, "messages": messages,
                   "max_tokens": max_tokens, "temperature": temperature,
                   "stream": False}
        payload.update(self.extra_body)
        return "/chat/completions", payload

    def _parse_chat_response(self, data):
        """按 provider 解析 (content, finish_reason, timings)。"""
        if self.provider == "ollama":
            msg = data.get("message") or {}
            content = (msg.get("content") or "").strip()
            finish = "length" if data.get("done_reason") == "length" else "stop"
            timings = {
                "prompt_tokens": data.get("prompt_eval_count"),
                "completion_tokens": data.get("eval_count"),
                "total_tokens": ((data.get("prompt_eval_count") or 0)
                                 + (data.get("eval_count") or 0)) or None,
            }
            return content, finish, timings
        usage = data.get("usage") or {}
        timings = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
        choice = (data.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        return content, (choice.get("finish_reason") or "stop"), timings

    async def chat(self, messages, max_tokens=512, temperature=0.2):
        """按 provider 调用推理端点；解析 content 与 finish_reason，
        绝不静默返回空串。

        5xx / 429 / 连接类错误重试一次（不重复提交已成功的生成）。
        """
        path, payload = self._chat_request(messages, max_tokens, temperature)
        c = await self._ensure_client()
        started = time.time()
        last_exc = None
        for attempt in (1, 2):
            try:
                r = await c.post(self.base_url + path,
                                 json=payload, headers=self._headers())
                if r.status_code == 200:
                    data = r.json()
                    content, finish, timings = self._parse_chat_response(data)
                    timings["elapsed_ms"] = round((time.time() - started) * 1000, 1)
                    self.last_timings = timings
                    self.last_finish_reason = finish
                    self.last_error = ""
                    self._log_line(
                        f"[api] chat ok finish={finish} "
                        f"chars={len(content)} elapsed_ms={timings['elapsed_ms']}")
                    if content:
                        return content
                    self.last_error = "empty content from api model"
                    raise RuntimeError(
                        f"empty output from {self.name} ({self.model}): "
                        f"{self.last_error}")
                self.last_error = f"chat {r.status_code}: {r.text[:200]}"
                if r.status_code >= 500 or r.status_code == 429:
                    if attempt == 1:
                        await asyncio.sleep(1.0)
                        continue
                raise RuntimeError(
                    f"{self.name} api error {r.status_code}: {r.text[:200]}")
            except RuntimeError:
                raise
            except (httpx.ConnectError, httpx.ReadTimeout,
                    httpx.RemoteProtocolError, httpx.ConnectTimeout) as exc:
                self.last_error = f"chat exception: {type(exc).__name__}: {exc}"
                last_exc = exc
                if attempt == 1:
                    await asyncio.sleep(1.0)
                    continue
                raise
            except Exception as exc:
                self.last_error = f"chat exception: {type(exc).__name__}: {exc}"
                last_exc = exc
                break
        raise RuntimeError(f"{self.name} chat failed: {self.last_error or last_exc}")

    async def stop(self):
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


def create_backend(slot_key, meta, port):
    """槽位后端唯一工厂：local -> LlamaServer 进程，api -> ApiBackend HTTP 客户端。"""
    if meta.get("type") == "api":
        return ApiBackend(
            slot_key, meta["base_url"], meta["model_ref"],
            api_key=config.api_key_for(meta),
            context_window=meta.get("context_window"),
            provider=meta.get("provider") or "openai",
            extra_body=meta.get("extra_body") or {},
        )
    return LlamaServer(slot_key, port, meta["path"], meta["devices"],
                       split_ratio=meta.get("split_ratio"))

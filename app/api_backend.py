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

# 上游「上下文超限」响应特征（各厂商措辞不同，宽松匹配）
_CTX_OVERFLOW_PATTERNS = (
    "context length", "context_length", "context window", "maximum context",
    "reduce the length", "input length exceeds", "too large",
    "exceeds the maximum", "上下文长度", "超出上下文", "上下文超限",
)


def _ctx_overflow(body):
    low = (body or "").lower()
    return any(p in low for p in _CTX_OVERFLOW_PATTERNS)


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
        # 自动探测上游上下文长度（默认启用）：探测结果进入预算与阈值计算
        await self._detect_context()

    async def _detect_context(self):
        """探测上游真实可用的 context length（默认启用）。

        ollama 探测分两层，**运行时窗口优先**：
        - /api/ps 中已加载实例的 context_length = 服务端实际分配的窗口
          （OLLAMA_CONTEXT_LENGTH / num_ctx 生效值，权威）；
        - /api/show 的 model_info *.context_length = 模型架构上限，
          仅在实例未加载时作为上界兜底。
        openai：无标准探测端点，沿用声明值，运行期由 clamp_slot_context
        兜底。探测结果经 config.record_detected_context 计算「生效窗口」：
        effective = min(声明值, 探测值)；未声明时 = 探测值。
        """
        if self.provider != "ollama":
            return
        try:
            runtime = await self._query_runtime_ctx()
            if runtime:
                config.record_detected_context(
                    self.name, runtime, "auto(ollama /api/ps)")
                self._log_line(f"[api] context detected(runtime): {runtime} "
                               f"tokens (/api/ps)")
                print(f"[ctx] slot {self.name} runtime context {runtime} "
                      f"tokens", flush=True)
                return
            # 实例未加载（懒加载）：先用架构上限兜底，首次成功对话后会
            # 再同步一次 /api/ps 的运行时值
            c = await self._ensure_client()
            r = await c.post(self.base_url + "/api/show",
                             json={"model": self.model},
                             headers=self._headers(), timeout=15)
            if r.status_code != 200:
                self._log_line(f"[api] context detect skipped: /api/show "
                               f"{r.status_code}")
                return
            info = (r.json().get("model_info") or {})
            arch = next((int(v) for k, v in info.items()
                         if k.endswith(".context_length")), None)
            if arch:
                config.record_detected_context(
                    self.name, arch, "auto(arch max, 待 /api/ps 校准)")
                self._log_line(f"[api] context detected(arch upper bound): "
                               f"{arch} tokens；运行时窗口将在首次推理后校准")
                print(f"[ctx] slot {self.name} arch context upper bound "
                      f"{arch} tokens（运行时待校准）", flush=True)
        except Exception as exc:
            self._log_line(f"[api] context detect failed: {type(exc).__name__}: "
                           f"{str(exc)[:120]}")

    async def _query_runtime_ctx(self):
        """/api/ps：返回本槽位模型已加载实例的实际分配窗口（未加载返回 None）。"""
        try:
            c = await self._ensure_client()
            r = await c.get(self.base_url + "/api/ps", timeout=5)
            if r.status_code != 200:
                return None
            for m in (r.json().get("models") or []):
                name = (m.get("name") or m.get("model") or "")
                if name.split(":")[0] == self.model.split(":")[0] and m.get("context_length"):
                    return int(m["context_length"])
        except Exception:
            pass
        return None

    async def sync_runtime_ctx_after_inference(self):
        """首次成功推理后校准：此时模型必然已加载，/api/ps 的运行时窗口
        是权威值（覆盖启动期用架构上限兜底的临时生效值）。"""
        if self.provider != "ollama":
            return
        runtime = await self._query_runtime_ctx()
        if runtime:
            config.record_detected_context(
                self.name, runtime, "auto(ollama /api/ps)")
            self._log_line(f"[api] runtime ctx calibrated: {runtime} tokens")

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

    async def _post_inference_guard(self, timings):
        """ollama 推理成功后的两道护栏（静默截断是 ollama 的默认行为，
        不报错——只能从用量侧检测）：

        1. 运行时窗口持续校准：**每次**成功推理后都从 /api/ps 重读运行时
           窗口（模型加载时的实际分配值）——用户在 Ollama 侧修改
           OLLAMA_CONTEXT_LENGTH 并触发模型重载后，路由器会自动跟踪，
           无需重启；查询仅毫秒级，相对推理耗时可忽略。
        2. 静默截断检测：prompt_tokens 逼近/达到生效窗口时，说明本次
           输入被服务端截断——把生效窗口收敛到实测容量。
        """
        eff = (config.SLOT_CTX_EFFECTIVE.get(self.name) or {})
        eff_now = int(eff.get("effective") or 0)
        pt = int((timings or {}).get("prompt_tokens") or 0)
        await self.sync_runtime_ctx_after_inference()
        if pt and eff_now and pt >= int(eff_now * 0.95):
            config.record_detected_context(
                self.name, pt, "auto(observed truncation cap)")
            self._log_line(f"[api] silent truncation detected: prompt_tokens "
                           f"{pt} >= {eff_now}; effective ctx -> {pt}")
            print(f"[ctx] slot {self.name} silent truncation: effective ctx "
                  f"-> {pt} tokens", flush=True)

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
                    if self.provider == "ollama":
                        await self._post_inference_guard(timings)
                    if content:
                        return content
                    self.last_error = "empty content from api model"
                    raise RuntimeError(
                        f"empty output from {self.name} ({self.model}): "
                        f"{self.last_error}")
                self.last_error = f"chat {r.status_code}: {r.text[:200]}"
                # 上游报上下文超限：把生效窗口对半收缩自适应，后续预算/阈值
                # 随之收紧，避免客户端反复撞墙
                if r.status_code in (400, 413) and _ctx_overflow(r.text):
                    config.clamp_slot_context(self.name, r.text[:120])
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

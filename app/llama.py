"""模型运行时进程封装。

设备、分层比例、KV 精度、端口全部来自 settings.yaml（经 config 解析），
代码不写死 GPU 编号、卡数或后端环境变量名。
"""
import asyncio
import os
import signal
import socket
import subprocess
import time

import httpx

from . import config


class LlamaServer:
    def __init__(self, name, port, model_path, devices, split_ratio=None):
        self.name = name
        self.port = port
        self.model_path = model_path
        self.devices = list(devices or [])
        self.split_ratio = split_ratio
        self.proc = None
        self.started_at = None
        self.load_ms = None
        self.last_error = ""
        self.last_timings = {}
        # 生成结束原因：stop / length。length 表示被 max_tokens 截断，
        # 必须透传给调用方，否则客户端会把半句话当成完整回答。
        self.last_finish_reason = None
        self._log = None

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def _cmd(self):
        cmd = [
            config.LLAMA_SERVER,
            "-m", str(self.model_path),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-c", str(config.NUM_CTX),
            "-ngl", str(config.N_GPU_LAYERS),
            "-np", "1",
            "--jinja",
            "-ctk", config.KV_CACHE, "-ctv", config.KV_CACHE,
        ]
        # 仅多卡且配置给出比例时才分层；单卡设备自动退化为单卡
        if len(self.devices) > 1 and self.split_ratio:
            cmd += ["-sm", "layer", "-ts", ",".join(str(x) for x in self.split_ratio)]
        return cmd

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _port_in_use(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                return s.connect_ex(("127.0.0.1", self.port)) == 0
        except Exception:
            return False

    def _log_tail(self, lines=8):
        try:
            path = config.LOGS / f"{self.name}.log"
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4096))
                text = f.read().decode("utf-8", "replace")
            return " | ".join(text.strip().splitlines()[-lines:])[-700:]
        except Exception:
            return ""

    async def start(self):
        if not self.model_path.exists():
            raise RuntimeError(f"model file missing: {self.model_path}")
        plan = config.VRAM_PLAN.get(self.name)
        if plan is not None and not plan.get("available", True):
            raise RuntimeError(f"{self.name} unavailable: {plan.get('reason')}")
        # 运行期复查：启动后可能有别的进程占用显存，加载前再看一次
        fits, why = config.slot_fits_now(self.name)
        if not fits:
            raise RuntimeError(f"{self.name} unavailable now: {why}")
        # 端口被其它进程占用时直接报清楚，避免起不来还误判为模型问题
        if self._port_in_use():
            raise RuntimeError(f"{self.name}: port {self.port} already in use")
        env = os.environ.copy()
        if self.devices:
            idx = ",".join(str(d) for d in self.devices)
            if config.GPU_BACKEND == "rocm":
                env["HIP_VISIBLE_DEVICES"] = idx
                env["ROCR_VISIBLE_DEVICES"] = idx
            else:
                env["CUDA_VISIBLE_DEVICES"] = idx
        if config.LLAMA_LIB_DIR:
            env["LD_LIBRARY_PATH"] = config.LLAMA_LIB_DIR + ":" + env.get("LD_LIBRARY_PATH", "")
        env["GGML_CUDA_NO_PINNED"] = "1"
        self._log = open(config.LOGS / f"{self.name}.log", "ab", buffering=0)
        self.proc = subprocess.Popen(
            self._cmd(), env=env, stdout=self._log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.started_at = time.time()
        try:
            await self.wait_ready()
        except Exception:
            await self.stop()
            raise
        self.load_ms = round((time.time() - self.started_at) * 1000, 1)

    async def wait_ready(self):
        deadline = time.time() + config.STARTUP_TIMEOUT_S
        last = ""
        async with httpx.AsyncClient(timeout=5) as c:
            while time.time() < deadline:
                if not self.alive():
                    raise RuntimeError(
                        f"{self.name} exited early, log: {self._log_tail()}")
                try:
                    r = await c.get(self.base + "/health")
                    if r.status_code == 200:
                        return
                    last = f"health={r.status_code}"
                except Exception as exc:
                    last = str(exc)[:120]
                await asyncio.sleep(1.5)
        raise TimeoutError(
            f"{self.name} not ready in {config.STARTUP_TIMEOUT_S}s ({last}); "
            f"log: {self._log_tail()}")

    async def healthy(self):
        if not self.alive():
            return False
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                return (await c.get(self.base + "/health")).status_code == 200
        except Exception:
            return False

    def _flatten(self, messages):
        out = []
        for m in messages:
            out.append(f"{m.get('role', 'user')}: {m.get('content', '')}")
        return "\n".join(out) + "\nassistant: "

    async def chat(self, messages, max_tokens=512, temperature=0.2):
        """关闭 thinking；正文为空降级 /completion；绝不静默返回空串。

        连接类错误与 5xx 重试一次（不重复提交已成功的生成）。
        """
        payload = {"messages": messages, "max_tokens": max_tokens,
                   "temperature": temperature, "stream": False,
                   "chat_template_kwargs": {"enable_thinking": False}}
        async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT_S) as c:
            for attempt in (1, 2):
                try:
                    r = await c.post(self.base + "/v1/chat/completions", json=payload)
                    if r.status_code == 200:
                        data = r.json()
                        self.last_timings = data.get("timings") or {}
                        choice = (data.get("choices") or [{}])[0]
                        self.last_finish_reason = choice.get("finish_reason")
                        msg = choice.get("message", {})
                        content = (msg.get("content") or "").strip()
                        if content:
                            return content
                        self.last_error = "empty content (reasoning_len=%d)" % len(
                            msg.get("reasoning_content") or "")
                        break
                    self.last_error = f"chat {r.status_code}: {r.text[:200]}"
                    if r.status_code >= 500 and attempt == 1:
                        await asyncio.sleep(1.0)
                        continue
                    break
                except (httpx.ConnectError, httpx.ReadTimeout,
                        httpx.RemoteProtocolError, httpx.ConnectTimeout) as exc:
                    self.last_error = f"chat exception: {type(exc).__name__}: {exc}"
                    if attempt == 1:
                        await asyncio.sleep(1.0)
                        continue
                    raise
                except Exception as exc:
                    self.last_error = f"chat exception: {type(exc).__name__}: {exc}"
                    break

            r = await c.post(self.base + "/completion", json={
                "prompt": self._flatten(messages), "n_predict": max_tokens,
                "temperature": temperature, "stream": False, "cache_prompt": True,
            })
            r.raise_for_status()
            data = r.json()
            self.last_timings = data.get("timings") or self.last_timings
            # /completion 用 stopped_limit 表示触顶；没有该字段时按正常结束
            self.last_finish_reason = ("length" if data.get("stopped_limit")
                                       else "stop")
            content = (data.get("content") or "").strip()
            if content:
                return content
            raise RuntimeError(f"empty output from {self.name}: {self.last_error}")

    async def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.to_thread(self.proc.wait, 20)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.to_thread(self.proc.wait, 5)
                except Exception:
                    pass
        self.proc = None
        if self._log:
            try:
                self._log.close()
            except Exception:
                pass
            self._log = None

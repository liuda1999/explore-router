"""本地 OpenAI 兼容假服务端：用于 api 槽位 / api 前台链路测试。

这不是对被测代码打桩——它是一个独立进程里的**真实 HTTP 服务器**，
ApiBackend 与主服务的全部网络路径都真实走一遍。仅标准库，无额外依赖。

行为标记（出现在任一 message 文本中即触发）：
- TRIGGER_500     -> 本请求返回 500（重试语义验证）
- TRIGGER_LENGTH  -> finish_reason=length（截断可见性）
- SLOW:<秒>       -> 延迟指定秒数后回复（超时语义）
- REM-xxxx        -> 回复**逐字回显**全部 REM- 标记（多轮召回验证）
- 默认            -> 按 model 分前缀的确定性回复（front / 其它槽位可区分）

用法: python3 tests/fake_openai_server.py --port 18900
"""
import argparse
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = time.time()
SEQ = {"n": 0}


def _texts(body):
    msgs = body.get("messages") or []
    out = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return "\n".join(out)


def _reply_text(model, text):
    marks = re.findall(r"REM-[A-Za-z0-9]+", text)
    mark_part = (" 标记:" + ",".join(marks)) if marks else ""
    prefix = "FRONT-OK" if model.startswith("fake-front") else "CLOUD-OK"
    return f"{prefix} model={model} chars={len(text)}{mark_part}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):   # 静默默认访问日志
        pass

    def _json(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "fake-front", "object": "model"},
                {"id": "fake-cloud", "object": "model"}]})
        elif self.path.endswith("/health"):
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": {"message": "bad json"}})
            return
        text = _texts(body)
        model = str(body.get("model") or "unknown")

        if "TRIGGER_500" in text:
            self._json(500, {"error": {"message": "injected failure"}})
            return
        if "SLOW:" in text:
            m = re.search(r"SLOW:(\d+)", text)
            time.sleep(min(60, int(m.group(1)) if m else 1))
        if "TRIGGER_LENGTH" in text:
            reply = f"{model} 被截断的半句话…"
            finish = "length"
        else:
            SEQ["n"] += 1
            reply = _reply_text(model, text)
            finish = "stop"
        self._json(200, {
            "id": f"chatcmpl-fake-{SEQ['n']}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": finish,
            }],
            "usage": {"prompt_tokens": len(text), "completion_tokens": len(reply),
                      "total_tokens": len(text) + len(reply)},
        })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18900)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake openai server on {args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""嵌入层：语义拆解（向量 + 结构化线索），失败时降级为纯文本规则。

两种 provider：
- onnx（默认）：本地 ONNX 嵌入模型（如 fast-bge-base-zh-v1.5），CPU 执行不占显存；
- ollama：本机/远端 Ollama 的嵌入模型（如 bge-m3），走 /api/embed——无本地模型
  文件、无 numpy/onnxruntime 依赖的开发机也能启用嵌入功能。

失败一律降级（enabled=False），保证链路不中断。
"""
import json
import math
import re
import urllib.request

from . import config


class EmbeddingProvider:
    def __init__(self):
        self.enabled = False
        self.error = ""
        self.dim = 0
        self.provider = getattr(config, "EMBED_PROVIDER", "onnx")
        self.ollama_url = getattr(config, "EMBED_OLLAMA_URL",
                                  "http://127.0.0.1:11434")
        self.ollama_model = getattr(config, "EMBED_OLLAMA_MODEL", "bge-m3")
        if self.provider == "ollama":
            try:
                # 启动自检：真实嵌入一次，验证模型可用并确定维度
                vec = self._ollama_embed("embedding 就绪自检")
                self.dim = len(vec)
                self.enabled = True
            except Exception as exc:
                self.error = f"ollama({self.ollama_model}): {type(exc).__name__}: {str(exc)[:160]}"
            return
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
            self.np = np
            self.tok = Tokenizer.from_file(str(config.EMBED_TOKENIZER))
            self.tok.enable_truncation(max_length=512)
            self.sess = ort.InferenceSession(
                str(config.EMBED_ONNX), providers=["CPUExecutionProvider"])
            self.input_names = [i.name for i in self.sess.get_inputs()]
            self.output_names = [o.name for o in self.sess.get_outputs()]
            self.enabled = True
            # 启动自检：跑一次真实前向，既验证模型可用，也让 /health 立刻有维度
            self.embed("embedding 就绪")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def _ollama_embed(self, text):
        """Ollama /api/embed：{"model","input":[text]} -> {"embeddings":[[...]]}。"""
        req = urllib.request.Request(
            self.ollama_url + "/api/embed",
            data=json.dumps({"model": self.ollama_model,
                             "input": [text[:2000]]}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        embs = d.get("embeddings") or []
        if not embs:
            raise RuntimeError(f"empty embeddings: {str(d)[:120]}")
        vec = [float(x) for x in embs[0]]
        norm = math.sqrt(sum(x * x for x in vec)) + 1e-9
        return [x / norm for x in vec]

    def embed(self, text):
        if not self.enabled:
            return None
        if self.provider == "ollama":
            try:
                vec = self._ollama_embed(text)
                self.dim = len(vec)
                self.error = ""
                return vec
            except Exception as exc:
                self.error = f"ollama embed: {type(exc).__name__}: {str(exc)[:120]}"
                return None
        try:
            np = self.np
            enc = self.tok.encode(text[:1000])
            ids = np.array([enc.ids], dtype=np.int64)
            mask = np.ones_like(ids)
            feed = {}
            for name in self.input_names:
                low = name.lower()
                if "input_ids" in low or low in ("input", "input_id"):
                    feed[name] = ids
                elif "attention" in low:
                    feed[name] = mask
                elif "token_type" in low or "segment" in low:
                    feed[name] = np.zeros_like(ids)
            if len(feed) < len(self.input_names):
                for name in self.input_names:
                    if name not in feed:
                        feed[name] = ids
            out = self.sess.run(None, feed)[0]
            if out.ndim == 3:
                vec = out[0].mean(axis=0)
            elif out.ndim == 2:
                vec = out[0]
            else:
                vec = out.reshape(-1)
            vec = vec.astype("float32")
            norm = float(np.linalg.norm(vec)) + 1e-9
            self.dim = int(vec.shape[0])
            return (vec / norm).tolist()
        except Exception as exc:
            self.error = f"embed runtime: {exc}"
            return None

    def split(self, text):
        """语义拆解：向量 + 结构化线索，供 4B 理解与后台任务包使用。"""
        vec = self.embed(text)
        words = [w for w in re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fa5]{2,6}", text)]
        seen, keywords = set(), []
        for w in words:
            if w not in seen:
                seen.add(w)
                keywords.append(w)
            if len(keywords) >= 8:
                break
        return {
            "char_len": len(text),
            "keywords": keywords,
            "has_code_hint": any(w in text.lower() for w in ("代码", "python", "code", "函数", "def ")),
            "vector_dim": self.dim if vec else 0,
            "vector_available": vec is not None,
            "vector_head": vec[:8] if vec else [],
        }

    def describe(self):
        d = {"enabled": self.enabled, "dim": self.dim, "error": self.error,
             "provider": self.provider}
        if self.provider == "ollama":
            d["model"] = self.ollama_model
            d["url"] = self.ollama_url
        else:
            d["inputs"] = getattr(self, "input_names", [])
            d["outputs"] = getattr(self, "output_names", [])
        return d

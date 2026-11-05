"""嵌入层：NAS 提供的 fast-bge-base-zh-v1.5 ONNX 模型。

做语义拆解（向量 + 结构化线索），失败时降级为纯文本规则，保证链路不中断。
"""
import json
import re
from . import config


class EmbeddingProvider:
    def __init__(self):
        self.enabled = False
        self.error = ""
        self.dim = 0
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

    def embed(self, text):
        if not self.enabled:
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
        return {"enabled": self.enabled, "dim": self.dim, "error": self.error,
                "inputs": getattr(self, "input_names", []), "outputs": getattr(self, "output_names", [])}

"""explore 配置入口。

唯一真源是工作区根目录的 settings.yaml：
- slots 是「角色槽位」，type 决定后端形态：local（本地 GGUF + llama-server 进程）
  或 api（OpenAI 兼容线上 API，不占本地显存）；
- 模型文件 / 量化 / 显存预算 / 设备分配 / base_url 全部来自配置；
- GPU 数量、品牌、后端由 gpu.snapshot() 运行时探测，代码不假设双卡或 NVIDIA；
- settings.local.yaml（可选）是运行期参数覆盖层：控制台/CLI 热调参数写入这里，
  重启后仍生效；主文件注释不受破坏；
- 环境变量仅作部署期覆盖，优先级最高，便于同一份配置跑在不同设备上。

改模型 / 换设备只改 settings.yaml，不改本文件。
"""
import copy
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import yaml

from . import gpu

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
# 数据与日志目录可用环境变量覆盖，测试/CI 用临时目录，避免污染生产状态
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
LOGS = Path(os.getenv("LOG_DIR", str(ROOT / "logs")))
DATA.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", str(ROOT / "settings.yaml")))
if not SETTINGS_FILE.exists():
    raise RuntimeError(f"settings.yaml not found: {SETTINGS_FILE}")
with open(SETTINGS_FILE, "r", encoding="utf-8") as _f:
    _S = yaml.safe_load(_f) or {}

# ---- 运行期参数覆盖层（settings.local.yaml）--------------------------------
# 控制台/CLI 热调的参数会写入该文件；import 时深合并到 settings.yaml 之上。
# 环境变量优先级仍高于覆盖层。文件由程序管理，手工编辑也允许（重启后生效）。
OVERLAY_FILE = Path(os.getenv(
    "SETTINGS_LOCAL_FILE", str(SETTINGS_FILE.parent / "settings.local.yaml")))
OVERLAY_LOADED = {}
try:
    if OVERLAY_FILE.exists():
        with open(OVERLAY_FILE, "r", encoding="utf-8") as _of:
            OVERLAY_LOADED = yaml.safe_load(_of) or {}
        if not isinstance(OVERLAY_LOADED, dict):
            raise RuntimeError(f"覆盖层必须是 YAML 映射: {OVERLAY_FILE}")
except yaml.YAMLError as _oe:
    raise RuntimeError(f"覆盖层 YAML 解析失败 {OVERLAY_FILE}: {_oe}")


_S_BASE = copy.deepcopy(_S)   # 覆盖层合并前的 settings.yaml 原值，reset 恢复目标用它推导


def _deep_merge(base, over):
    """dict 深合并（子 dict 递归），标量与 list 直接覆盖。

    覆盖层的任意 dict 节点可含 `__remove__: [键名...]`，合并后从该节点删除
    对应键——控制台借此可以「删除」settings.yaml 里定义的槽位/条目。
    """
    remove = over.pop("__remove__", None) if isinstance(over, dict) else None
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    for k in (remove or []):
        base.pop(k, None)


# 合并必须用副本：_deep_merge 会 pop 掉 __remove__ 标记，若就地合并会吃掉
# OVERLAY_LOADED 里的删除标记，之后任何一次保存都会让「已彻底删除」的键复活
_deep_merge(_S, copy.deepcopy(OVERLAY_LOADED))


def _env(name, default=None):
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _fail(msg):
    raise RuntimeError(f"settings.yaml 配置错误: {msg}")


_runtime = _S.get("runtime", {}) or {}
_ports = _runtime.get("ports", {}) or {}

LLAMA_SERVER = _env("LLAMA_SERVER", _runtime.get("server_bin", "llama-server"))
LLAMA_LIB_DIR = _env("LLAMA_LIB_DIR", _runtime.get("lib_dir", ""))
API_PORT = int(_env("API_PORT", _ports.get("api", 8100)))
FRONT_PORT = int(_env("FRONT_PORT", _ports.get("front", 18100)))
SLOT_PORT = int(_env("SLOT_PORT", _ports.get("slot", 18101)))
STARTUP_TIMEOUT_S = int(_env("STARTUP_TIMEOUT_S", _runtime.get("startup_timeout_s", 420)))
REQUEST_TIMEOUT_S = int(_env("REQUEST_TIMEOUT_S", _runtime.get("request_timeout_s", 600)))
N_GPU_LAYERS = int(_env("N_GPU_LAYERS", _runtime.get("n_gpu_layers", 99)))
MAX_OUTPUT_TOKENS = int(_env("MAX_OUTPUT_TOKENS", _runtime.get("max_output_tokens", 1024)))
QUEUE_MAXSIZE = int(_env("QUEUE_MAXSIZE", _runtime.get("queue_maxsize", 32)))

# OpenAI 兼容层：非流式请求默认等待后台完成；超时后回退即时回复并给出 task_id。
# 负数按 0 处理（0 = 不等，立即返回即时回复）。
_openai = _S.get("openai", {}) or {}
OPENAI_WAIT_TIMEOUT_S = max(0, int(_env(
    "OPENAI_WAIT_TIMEOUT_S", _openai.get("wait_timeout_s", 120))))
OPENAI_DEFAULT_MODE = str(
    _env("OPENAI_DEFAULT_MODE", _openai.get("default_mode", "final"))).lower()
if OPENAI_DEFAULT_MODE not in ("final", "instant"):
    OPENAI_DEFAULT_MODE = "final"

# 硬件探测：不写死卡数 / 品牌 / 后端
GPU_BACKEND_PREF = str(_env("GPU_BACKEND", _runtime.get("gpu_backend", "auto"))).lower()
SNAP = gpu.snapshot(GPU_BACKEND_PREF)
GPU_BACKEND = SNAP.get("backend", "cpu")
DEVICE_COUNT = len(SNAP.get("gpus", []))
TOTAL_VRAM_MB = int(SNAP.get("total_mb", 0))

# 上下文：auto 按显存查表；整数直接采用；环境变量最高优先
_ctx = _S.get("context", {}) or {}
KV_CACHE = str(_env("KV_CACHE", _ctx.get("kv_cache", "f16")))
_raw_ctx = _ctx.get("num_ctx", "auto")


def _resolve_ctx():
    if isinstance(_raw_ctx, int):
        return _raw_ctx, "settings.yaml"
    for row in _ctx.get("auto_table", []) or []:
        if TOTAL_VRAM_MB >= int(row.get("min_total_mb", 0)):
            return int(row["num_ctx"]), f"auto(total_vram={TOTAL_VRAM_MB}MB)"
    return 4096, "auto(fallback)"


_AUTO_CTX, _CTX_SOURCE = _resolve_ctx()
_env_ctx = _env("NUM_CTX")
NUM_CTX = int(_env_ctx) if _env_ctx else _AUTO_CTX
NUM_CTX_SOURCE = "env:NUM_CTX" if _env_ctx else _CTX_SOURCE

# ---- 槽位设备分配 ----------------------------------------------------------
# type: local（默认）= 本地 GGUF，走 llama-server 进程；
# type: api = OpenAI 兼容线上 API，无本地显存与进程，不参与互斥槽位切换。
_slots_cfg = _S.get("slots", {}) or {}
if not _slots_cfg:
    _fail("slots 不能为空")

API_DEFAULT_CTX = 32768


def _base_devices(d):
    """按设备实际情况分配 GPU 索引；单卡自动退化为单卡，不假设双卡。"""
    placement = str(d.get("placement", "auto")).lower()
    n = DEVICE_COUNT
    if n == 0:
        return []                      # 无 GPU：交给运行时用 CPU
    if placement == "explicit":
        return [int(x) for x in (d.get("devices") or [0])]
    if placement == "spread" and n > 1:
        return list(range(n))
    if n == 1:
        return [0]
    if d.get("resident"):
        return [0]                     # 前台常驻第一张卡（只看 resident，不看 key 名）
    return [n - 1]                     # 其它单卡槽位放最后一张，避开前台


# 第一遍：先确定每个槽位用哪些卡，才能算出前台占用了哪张卡
MODELS_META = {}
for _key, _d in _slots_cfg.items():
    _type = str(_d.get("type", "local")).lower()
    if _type not in ("local", "api"):
        _fail(f"slots.{_key}.type 只支持 local | api: {_type}")
    _meta = {
        "type": _type,
        "tier": _d.get("role", _key),
        "quant": _d.get("quant", ""),
        "resident": bool(_d.get("resident", False)),
    }
    if _type == "api":
        _provider = str(_d.get("provider", "openai")).lower()
        if _provider not in ("openai", "ollama"):
            _fail(f"slots.{_key}.provider 只支持 openai | ollama: {_provider}")
        _extra = _d.get("extra_body")
        if _extra is not None and not isinstance(_extra, dict):
            _fail(f"slots.{_key}.extra_body 必须是映射: {_extra}")
        # context_window 缺省 None = 启动时自动探测上游（ollama /api/show）；
        # 显式声明则与探测值取 min 生效
        _cw = _d.get("context_window")
        _meta.update({
            "path": None,
            "provider": _provider,
            "extra_body": dict(_extra or {}),
            "model_ref": str(_d.get("model", "") or ""),   # 远端模型名（如 deepseek-chat）
            "base_url": str(_d.get("base_url", "") or "").rstrip("/"),
            "api_key": str(_d.get("api_key", "") or ""),
            "api_key_env": str(_d.get("api_key_env", "") or ""),
            "context_window": (int(_cw) if _cw not in (None, "") else None),
            "slot_max_tokens": _d.get("max_output_tokens"),
            "devices": [],
            "devices_str": "",
            "split_ratio": None,
            "weight_mb": 0,
            "vram_mb": 0,
        })
    else:
        _devs = _base_devices(_d)
        _meta.update({
            "path": ROOT / _d["model"],
            "model_ref": _d["model"],
            "devices": _devs,
            "devices_str": ",".join(str(x) for x in _devs),
            "split_ratio": None,
            "weight_mb": _d.get("weight_mb"),
            "vram_mb": _d.get("vram_mb"),
        })
    MODELS_META[_key] = _meta

_residents = [k for k, v in MODELS_META.items() if v["resident"]]
if len(_residents) != 1:
    _fail(f"resident: true 的槽位必须恰好一个，当前 {_residents or '无'}")
FRONT_KEY = _residents[0]
FRONT_TYPE = MODELS_META[FRONT_KEY]["type"]
FRONT_MODEL = MODELS_META[FRONT_KEY]["path"]           # local: GGUF 路径; api: None
FRONT_MODEL_REF = MODELS_META[FRONT_KEY]["model_ref"]  # 展示用：本地引用或远端模型名
BACKEND_SLOTS = [k for k in MODELS_META if k != FRONT_KEY]
FRONT_DEVICES = MODELS_META[FRONT_KEY]["devices"]
FRONT_DEVICE = FRONT_DEVICES[0] if FRONT_DEVICES else None
FRONT_WEIGHT_MB = int(MODELS_META[FRONT_KEY].get("weight_mb") or 0)


def api_key_for(meta):
    """解析 api 槽位密钥：api_key_env 环境变量优先，其次 settings.yaml 明文。
    只在服务进程内使用，绝不写日志或 /config。"""
    env_name = meta.get("api_key_env") or ""
    if env_name:
        v = os.getenv(env_name)
        if v:
            return v
    return meta.get("api_key") or ""


def _adjusted_snap():
    """自动分层比例前，先扣掉前台常驻模型在它那张卡上的预估占用。"""
    adj = {"gpus": [dict(g) for g in SNAP.get("gpus", [])]}
    if FRONT_DEVICE is None or not FRONT_WEIGHT_MB:
        return adj
    for g in adj["gpus"]:
        if int(g.get("index", -1)) == int(FRONT_DEVICE):
            g["free_mb"] = max(1, int(g.get("free_mb", 1)) - int(FRONT_WEIGHT_MB * 1.5))
            break
    return adj


# 第二遍：多卡槽位计算分层比例（api 槽位 devices 为空，自然跳过）
for _key, _meta in MODELS_META.items():
    if len(_meta["devices"]) <= 1:
        continue
    _d = _slots_cfg[_key]
    _explicit = _d.get("split_ratio")
    if _explicit:
        _ratio = [int(x) for x in _explicit]
    else:
        _ratio = gpu.split_ratio_for(_adjusted_snap(), _meta["devices"])
    _meta["split_ratio"] = [int(x) for x in _ratio] if _ratio else None

_routing = _S.get("routing", {}) or {}
TIER_TO_SLOT = dict(_routing.get("tier_to_slot", {}) or {})
DEFAULT_SLOT = _routing.get("default_slot") or (BACKEND_SLOTS[0] if BACKEND_SLOTS else FRONT_KEY)
COMPLEX_WORDS = list(_routing.get("complex_words", []) or [])
FRONT_SYSTEM = (_S.get("frontend", {}) or {}).get("system_prompt", "你是本地助手，请简洁回应。")
TIER_ORDER = [str(x) for x in (_routing.get("tier_order") or ["simple", "mid", "heavy"])]

# 分级阈值：tier_thresholds 为每档触发字符数（含第 4+ 档）；
# 未配置时前 3 档沿用 mid_min_chars / heavy_min_chars，保持向后兼容。
# MID_MIN_CHARS / HEAVY_MIN_CHARS 保留为旧字段镜像（写入热调时以 tier_thresholds 为准落盘）。
_thr_cfg = _routing.get("tier_thresholds") or {}
TIER_THRESHOLDS = {str(k): int(v) for k, v in _thr_cfg.items()}
MID_MIN_CHARS = int(_routing.get("mid_min_chars", 25))
HEAVY_MIN_CHARS = int(_routing.get("heavy_min_chars", 120))
if TIER_THRESHOLDS:
    MID_MIN_CHARS = int(TIER_THRESHOLDS.get(TIER_ORDER[1], MID_MIN_CHARS))
    HEAVY_MIN_CHARS = int(TIER_THRESHOLDS.get(TIER_ORDER[2], HEAVY_MIN_CHARS))
else:
    if len(TIER_ORDER) >= 2:
        TIER_THRESHOLDS[TIER_ORDER[1]] = MID_MIN_CHARS
    if len(TIER_ORDER) >= 3:
        TIER_THRESHOLDS[TIER_ORDER[2]] = HEAVY_MIN_CHARS
# 每档专属强词表：命中即归入该档（可选；全局 complex_words 依然全档生效）
TIER_COMPLEX_WORDS = {
    str(k): [str(w) for w in (v or [])]
    for k, v in (_routing.get("tier_complex_words") or {}).items()
}
# 每档降级链：tier -> 有序槽位列表；映射槽位不可用时依次尝试，缺省走 default_slot
TIER_FALLBACKS = {
    str(k): [str(s) for s in (v or [])]
    for k, v in (_routing.get("tier_fallbacks") or {}).items()
}

# 前台上限窗口：local 前台 = 全局 NUM_CTX；api 前台 = 该槽位声明的 context_window
# （未声明时先用默认值，启动探测后会自动更新为上游真实值）。
# 历史预算 / 输入上限由它推导，与本地后台槽位的 NUM_CTX 解耦。
FRONT_NUM_CTX = (NUM_CTX if FRONT_TYPE == "local"
                 else int(MODELS_META[FRONT_KEY]["context_window"]
                          or API_DEFAULT_CTX))

_emb = _S.get("embedding", {}) or {}
EMBED_PROVIDER = str(_env("EMBED_PROVIDER", _emb.get("provider", "onnx"))).lower()
if EMBED_PROVIDER not in ("onnx", "ollama"):
    _fail(f"embedding.provider 只支持 onnx | ollama: {EMBED_PROVIDER}")
EMBED_OLLAMA_URL = _env("EMBED_OLLAMA_URL", _emb.get("ollama_url", "http://127.0.0.1:11434"))
EMBED_OLLAMA_MODEL = _env("EMBED_OLLAMA_MODEL", _emb.get("ollama_model", "bge-m3"))
EMBED_ONNX = ROOT / _emb.get("onnx", "models/embedding/model.onnx")
EMBED_TOKENIZER = ROOT / _emb.get("tokenizer", "models/embedding/tokenizer.json")

# ---- 显存预算：不假设卡数，按实际可见设备判断槽位能否装下 ------------------
_budget = _S.get("budget", {}) or {}
RESERVE_SYSTEM_MB = int(_budget.get("reserve_system_mb", 1024))
HEADROOM_RATIO = float(_budget.get("headroom_ratio", 0.10))

# 前台与上下文预算 ----------------------------------------------------------
_front = _S.get("frontend", {}) or {}
FRONT_MAX_TOKENS = int(_env("FRONT_MAX_TOKENS", _front.get("max_tokens", 256)))
FRONT_RESTART_COOLDOWN_S = int(_front.get("restart_cooldown_s", 30))
PROGRESS_WINDOW_S = int(_front.get("progress_window_s", 1800))

# 历史预算按上下文自动计算：不再用固定条数，小上下文自动收紧
HISTORY_BUDGET_RATIO = float(_front.get("history_budget_ratio", 0.45))
HISTORY_MIN_CHARS = int(_front.get("history_min_chars", 2048))
HISTORY_MAX_MESSAGES = int(_front.get("history_max_messages", 40))
CONTEXT_SAFETY_TOKENS = int(_front.get("context_safety_tokens", 256))

# 上下文压缩：预算不足时先把将被丢弃的早期轮次摘要保留，再考虑丢弃
_comp = _ctx.get("compaction", {}) or {}
COMPACTION_ENABLED = str(_env(
    "COMPACTION_ENABLED", _comp.get("enabled", True))).lower() not in (
    "0", "false", "no", "off")
COMPACTION_MAX_SUMMARY_CHARS = max(80, int(_env(
    "COMPACTION_MAX_SUMMARY_CHARS", _comp.get("max_summary_chars", 800))))
COMPACTION_MIN_DROPPED_CHARS = max(0, int(_env(
    "COMPACTION_MIN_DROPPED_CHARS", _comp.get("min_dropped_chars", 600))))

# 按 1 字符≈1 token 保守估算，为输出、system、安全余量、最小历史预留
_MAX_INPUT_CHARS_EXPLICIT = None
_max_chars = _routing.get("max_input_chars")
if _max_chars not in (None, 0, "auto"):
    _MAX_INPUT_CHARS_EXPLICIT = int(_max_chars)


def front_ctx_available():
    """前台可用上下文（字符估算）= 前台窗口 - 输出 - 安全余量 - system。"""
    return FRONT_NUM_CTX - FRONT_MAX_TOKENS - CONTEXT_SAFETY_TOKENS - len(FRONT_SYSTEM)


def _compute_max_input_chars():
    if _MAX_INPUT_CHARS_EXPLICIT is not None:
        return _MAX_INPUT_CHARS_EXPLICIT
    # 输入上限 = 可用上下文 - 最小历史预算；保证任何输入都不会挤掉输出与安全余量
    return max(512, front_ctx_available() - HISTORY_MIN_CHARS)


_CTX_AVAILABLE = front_ctx_available()
if _CTX_AVAILABLE <= 0:
    _fail(f"前台窗口 {FRONT_NUM_CTX} 太小：扣除输出/安全余量/system 后无可用空间")
MAX_INPUT_CHARS = _compute_max_input_chars()


# ---- 启动期配置校验：错配在启动时报清楚，而不是运行中静默降级 -----------------
def _validate():
    if len(TIER_ORDER) < 3:
        _fail(f"routing.tier_order 至少需要 3 个档位（第 1 个=直接回答，其余=后台档），"
              f"当前 {TIER_ORDER}")
    if len(set(TIER_ORDER)) != len(TIER_ORDER):
        _fail(f"routing.tier_order 有重复档位: {TIER_ORDER}")
    missing = [t for t in TIER_ORDER if t not in TIER_TO_SLOT]
    if missing:
        _fail(f"routing.tier_to_slot 缺少档位映射: {missing}（当前 {sorted(TIER_TO_SLOT)}）")
    bad_slots = {t: s for t, s in TIER_TO_SLOT.items() if s not in MODELS_META}
    if bad_slots:
        _fail(f"routing.tier_to_slot 指向不存在的槽位: {bad_slots}")
    if DEFAULT_SLOT not in MODELS_META:
        _fail(f"routing.default_slot 不存在: {DEFAULT_SLOT}")
    # 分级阈值：沿 tier_order 严格递增（第 1 档隐式为 0）
    _prev = 0
    for _t in TIER_ORDER[1:]:
        _v = TIER_THRESHOLDS.get(_t)
        if _v is None:
            _fail(f"档位 {_t} 缺少分级阈值（routing.tier_thresholds 或 "
                  f"mid/heavy_min_chars）")
        if _v <= _prev:
            _fail(f"分级阈值必须沿 tier_order 严格递增: {_t}={_v} (前值 {_prev})")
        _prev = _v
    bad_fb = {t: [s for s in v if s not in MODELS_META]
              for t, v in TIER_FALLBACKS.items() if t not in TIER_ORDER
              or any(s not in MODELS_META for s in v)}
    if bad_fb:
        _fail(f"routing.tier_fallbacks 指向不存在的档位或槽位: {bad_fb}")
    if not (0 < MID_MIN_CHARS < HEAVY_MIN_CHARS):
        _fail(f"分级阈值需满足 0 < mid({MID_MIN_CHARS}) < heavy({HEAVY_MIN_CHARS})")
    if not (0 < HISTORY_BUDGET_RATIO < 0.9):
        _fail(f"frontend.history_budget_ratio 必须在 (0, 0.9) 内: {HISTORY_BUDGET_RATIO}")
    if not (0 <= HEADROOM_RATIO < 0.5):
        _fail(f"budget.headroom_ratio 必须在 [0, 0.5) 内: {HEADROOM_RATIO}")
    if RESERVE_SYSTEM_MB < 0:
        _fail(f"budget.reserve_system_mb 不能为负: {RESERVE_SYSTEM_MB}")
    if HISTORY_MIN_CHARS < 128:
        _fail(f"frontend.history_min_chars 太小: {HISTORY_MIN_CHARS}")
    if HISTORY_MAX_MESSAGES < 2:
        _fail(f"frontend.history_max_messages 至少为 2: {HISTORY_MAX_MESSAGES}")
    reserved = (MAX_INPUT_CHARS + HISTORY_MIN_CHARS + FRONT_MAX_TOKENS
                + CONTEXT_SAFETY_TOKENS + len(FRONT_SYSTEM))
    if MAX_INPUT_CHARS < 128 or reserved > FRONT_NUM_CTX:
        _fail(f"上下文预算超出前台窗口 {FRONT_NUM_CTX}: 输入上限 {MAX_INPUT_CHARS} + "
              f"最小历史 {HISTORY_MIN_CHARS} + 输出 {FRONT_MAX_TOKENS} + "
              f"安全余量 {CONTEXT_SAFETY_TOKENS} + system {len(FRONT_SYSTEM)} "
              f"= {reserved}")
    for k, v in MODELS_META.items():
        if v["type"] == "api":
            if not v["base_url"]:
                _fail(f"slots.{k}.base_url 不能为空（type: api）")
            if not v["base_url"].startswith(("http://", "https://")):
                _fail(f"slots.{k}.base_url 必须以 http:// 或 https:// 开头: "
                      f"{v['base_url']}")
            if not v["model_ref"]:
                _fail(f"slots.{k}.model 不能为空（type: api 填远端模型名，如 deepseek-chat）")
            if v.get("context_window") is not None and v["context_window"] < 1024:
                _fail(f"slots.{k}.context_window 太小: {v['context_window']}")
            if v.get("slot_max_tokens") is not None and int(v["slot_max_tokens"]) <= 0:
                _fail(f"slots.{k}.max_output_tokens 必须为正数: {v['slot_max_tokens']}")
            continue
        if not v["model_ref"]:
            _fail(f"slots.{k}.model 不能为空")
        if DEVICE_COUNT and v["devices"]:
            bad = [d for d in v["devices"] if d < 0 or d >= DEVICE_COUNT]
            if bad:
                _fail(f"slots.{k}.devices 超出实际卡数 {DEVICE_COUNT}: {bad}")
        if v["split_ratio"] is not None and (
                len(v["split_ratio"]) != len(v["devices"]) or
                any(x <= 0 for x in v["split_ratio"])):
            _fail(f"slots.{k}.split_ratio 必须与 devices 等长且为正数: "
                  f"{v['split_ratio']} vs devices={v['devices']}")
        if v["vram_mb"] is not None and int(v["vram_mb"]) <= 0:
            _fail(f"slots.{k}.vram_mb 必须为正数: {v['vram_mb']}")


_validate()


# ---- 运行期可热调参数（白名单）---------------------------------------------
# 控制台 / CLI / POST /admin/config 可修改；立即生效并写入 settings.local.yaml。
# 未列入白名单的（槽位/模型/端口/num_ctx/KV 精度等）为启动期参数，需改文件重启。
def _cast_int(v):
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            if isinstance(v, float) and v == int(v):
                return int(v)
            if isinstance(v, str):
                return int(v.strip())
        except (ValueError, TypeError):
            raise ValueError(f"需要整数，得到 {v!r}")
        raise ValueError(f"需要整数，得到 {v!r}")
    return v


def _cast_float(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            return float(str(v).strip())
        except (ValueError, TypeError):
            raise ValueError(f"需要数字，得到 {v!r}")
    return float(v)


def _cast_str(v):
    if isinstance(v, bool) or not isinstance(v, str):
        raise ValueError(f"需要字符串，得到 {v!r}")
    return v


def _cast_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
        return v.strip().lower() in ("true", "1", "yes")
    raise ValueError(f"需要布尔值，得到 {v!r}")


def _cast_str_list(v):
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise ValueError(f"需要字符串列表，得到 {v!r}")
    return [x for x in v if x.strip()]


def _cast_int_dict(v):
    if not isinstance(v, dict) or not v:
        raise ValueError(f"需要非空映射（档位 -> 字符数），得到 {v!r}")
    out = {}
    for k, x in v.items():
        out[str(k)] = _cast_int(x)
    return out


def _cast_str_dict_list(v):
    if not isinstance(v, dict):
        raise ValueError(f"需要映射（档位 -> 词表），得到 {v!r}")
    return {str(k): _cast_str_list(x) for k, x in v.items()}


def _min_checked(v, lo):
    if v < lo:
        raise ValueError(f"不能小于 {lo}: {v}")
    return v


def _int_min(lo):
    def _cast(v):
        return _min_checked(_cast_int(v), lo)
    return _cast


HOT_PARAMS = {
    # name: {cast, yaml(覆盖层落盘路径), after(应用后校验), label}
    "MID_MIN_CHARS": {
        "cast": _int_min(1),
        "yaml": ("routing", "tier_thresholds"), "group": "routing",
        "label": "中档触发字符数（同步进 tier_thresholds）"},
    "HEAVY_MIN_CHARS": {
        "cast": _int_min(1),
        "yaml": ("routing", "tier_thresholds"), "group": "routing",
        "label": "重档触发字符数（同步进 tier_thresholds）"},
    "TIER_THRESHOLDS": {
        "cast": _cast_int_dict,
        "yaml": ("routing", "tier_thresholds"), "group": "routing",
        "label": "各档触发字符数映射"},
    "COMPLEX_WORDS": {
        "cast": _cast_str_list,
        "yaml": ("routing", "complex_words"), "group": "routing",
        "label": "复杂词表（全局）"},
    "TIER_COMPLEX_WORDS": {
        "cast": _cast_str_dict_list,
        "yaml": ("routing", "tier_complex_words"), "group": "routing",
        "label": "各档专属强词表"},
    "FRONT_MAX_TOKENS": {
        "cast": _int_min(16), "env": "FRONT_MAX_TOKENS",
        "yaml": ("frontend", "max_tokens"), "group": "frontend",
        "label": "即时回复生成上限（tokens）"},
    "FRONT_SYSTEM": {
        "cast": _cast_str,
        "yaml": ("frontend", "system_prompt"), "group": "frontend",
        "label": "前台 system 提示词"},
    "PROGRESS_WINDOW_S": {
        "cast": _int_min(0),
        "yaml": ("frontend", "progress_window_s"), "group": "frontend",
        "label": "进度提问关联任务的时间窗（秒）"},
    "HISTORY_BUDGET_RATIO": {
        "cast": _cast_float,
        "yaml": ("frontend", "history_budget_ratio"), "group": "frontend",
        "label": "历史预算占可用上下文比例"},
    "HISTORY_MIN_CHARS": {
        "cast": _int_min(128),
        "yaml": ("frontend", "history_min_chars"), "group": "frontend",
        "label": "历史最小字符预算"},
    "HISTORY_MAX_MESSAGES": {
        "cast": _int_min(2),
        "yaml": ("frontend", "history_max_messages"), "group": "frontend",
        "label": "历史条数上限"},
    "CONTEXT_SAFETY_TOKENS": {
        "cast": _int_min(0),
        "yaml": ("frontend", "context_safety_tokens"), "group": "frontend",
        "label": "token 估算误差与模板开销预留"},
    "COMPACTION_ENABLED": {
        "cast": _cast_bool, "env": "COMPACTION_ENABLED",
        "yaml": ("context", "compaction", "enabled"), "group": "compaction",
        "label": "是否开启历史压缩"},
    "COMPACTION_MAX_SUMMARY_CHARS": {
        "cast": _int_min(80), "env": "COMPACTION_MAX_SUMMARY_CHARS",
        "yaml": ("context", "compaction", "max_summary_chars"), "group": "compaction",
        "label": "压缩摘要长度上限（字符）"},
    "COMPACTION_MIN_DROPPED_CHARS": {
        "cast": _int_min(0), "env": "COMPACTION_MIN_DROPPED_CHARS",
        "yaml": ("context", "compaction", "min_dropped_chars"), "group": "compaction",
        "label": "被丢弃内容少于该值时不摘要"},
    "MAX_OUTPUT_TOKENS": {
        "cast": _int_min(64), "env": "MAX_OUTPUT_TOKENS",
        "yaml": ("runtime", "max_output_tokens"), "group": "runtime",
        "label": "后台任务单次生成上限（tokens）"},
    "REQUEST_TIMEOUT_S": {
        "cast": _int_min(10), "env": "REQUEST_TIMEOUT_S",
        "yaml": ("runtime", "request_timeout_s"), "group": "runtime",
        "label": "模型请求超时（秒）"},
    "OPENAI_WAIT_TIMEOUT_S": {
        "cast": _int_min(0), "env": "OPENAI_WAIT_TIMEOUT_S",
        "yaml": ("openai", "wait_timeout_s"), "group": "openai",
        "label": "final 模式等待后台完成上限（秒）"},
    "OPENAI_DEFAULT_MODE": {
        "cast": _cast_str,
        "yaml": ("openai", "default_mode"), "group": "openai",
        "label": "OpenAI 兼容层默认返回模式"},
}

# reset 的恢复目标：环境变量值（env 优先级最高，必须保留）> settings.yaml
# 合并覆盖层之前的原值 > import 时的生效值。显示「已改」与恢复语义都以它为准。
def _reset_target(name):
    spec = HOT_PARAMS[name]
    env_name = spec.get("env")
    if env_name:
        v = os.getenv(env_name)
        if v not in (None, ""):
            try:
                return spec["cast"](v)
            except ValueError:
                pass
    node = _S_BASE
    for p in spec["yaml"]:
        if not isinstance(node, dict) or p not in node:
            node = None
            break
        node = node[p]
    if node is not None:
        try:
            return spec["cast"](node)
        except ValueError:
            pass
    return copy.deepcopy(globals()[name])


HOT_PARAM_BASE = {k: _reset_target(k) for k in HOT_PARAMS}


def _check_hot_value(name, value):
    """单参数类型/范围校验，返回 (cast 后的值, 错误信息或 None)。"""
    try:
        v = HOT_PARAMS[name]["cast"](value)
    except ValueError as e:
        return None, str(e)
    if name == "OPENAI_DEFAULT_MODE" and str(v).lower() not in ("final", "instant"):
        return None, f"只支持 final | instant: {v}"
    if name == "HISTORY_BUDGET_RATIO" and not (0 < v < 0.9):
        return None, f"必须在 (0, 0.9) 内: {v}"
    return v, None


def _effective_state(cand):
    """把候选更新叠加到当前值上，返回 (阈值 dict, 交叉校验错误或 None)。"""
    thr = dict(TIER_THRESHOLDS)
    if "TIER_THRESHOLDS" in cand:
        thr = dict(cand["TIER_THRESHOLDS"])
    if "MID_MIN_CHARS" in cand and len(TIER_ORDER) >= 2:
        if "TIER_THRESHOLDS" in cand and thr.get(TIER_ORDER[1]) != cand["MID_MIN_CHARS"]:
            return None, (f"MID_MIN_CHARS({cand['MID_MIN_CHARS']}) 与 "
                          f"TIER_THRESHOLDS[{TIER_ORDER[1]}] 冲突，请只改一个")
        thr[TIER_ORDER[1]] = cand["MID_MIN_CHARS"]
    if "HEAVY_MIN_CHARS" in cand and len(TIER_ORDER) >= 3:
        if "TIER_THRESHOLDS" in cand and thr.get(TIER_ORDER[2]) != cand["HEAVY_MIN_CHARS"]:
            return None, (f"HEAVY_MIN_CHARS({cand['HEAVY_MIN_CHARS']}) 与 "
                          f"TIER_THRESHOLDS[{TIER_ORDER[2]}] 冲突，请只改一个")
        thr[TIER_ORDER[2]] = cand["HEAVY_MIN_CHARS"]
    prev = 0
    for t in TIER_ORDER[1:]:
        v = thr.get(t)
        if v is None:
            return None, f"档位 {t} 缺少分级阈值"
        if v <= prev:
            return None, f"阈值必须沿 tier_order 严格递增: {t}={v} (前值 {prev})"
        prev = v
    # 上下文预算交叉校验（前台窗口不变，输出/历史/安全余量可热调）
    g = {k: cand.get(k, globals()[k]) for k in
         ("FRONT_MAX_TOKENS", "HISTORY_MIN_CHARS", "CONTEXT_SAFETY_TOKENS")}
    eff_max_in = _MAX_INPUT_CHARS_EXPLICIT
    if eff_max_in is None:
        eff_avail = (FRONT_NUM_CTX - g["FRONT_MAX_TOKENS"] - g["CONTEXT_SAFETY_TOKENS"]
                     - len(cand.get("FRONT_SYSTEM", FRONT_SYSTEM)))
        eff_max_in = max(512, eff_avail - g["HISTORY_MIN_CHARS"])
    reserved = (eff_max_in + g["HISTORY_MIN_CHARS"] + g["FRONT_MAX_TOKENS"]
                + g["CONTEXT_SAFETY_TOKENS"]
                + len(cand.get("FRONT_SYSTEM", FRONT_SYSTEM)))
    if eff_max_in < 128 or reserved > FRONT_NUM_CTX:
        return None, (f"上下文预算超出前台窗口 {FRONT_NUM_CTX}: 输入上限 {eff_max_in} + "
                      f"最小历史 {g['HISTORY_MIN_CHARS']} + 输出 {g['FRONT_MAX_TOKENS']} + "
                      f"安全余量 {g['CONTEXT_SAFETY_TOKENS']} + system = {reserved}")
    return thr, None


def _atomic_write_yaml(path, data):
    """原子落盘：同目录临时文件 + os.replace，避免中途崩溃留下损坏的 YAML。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _load_overlay_data():
    if not OVERLAY_FILE.exists():
        return {}
    with open(OVERLAY_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


# 串行化所有覆盖层/热参数写入：调用方在事件循环内同步执行，当前结构天然串行；
# 这把锁是防御未来的（一旦函数内引入 await，竞态立即出现），同时保护 tmp 文件名唯一化。
_ADMIN_WRITE_LOCK = threading.Lock()


def _persist_overlay(values=None, remove=None):
    """把 {参数名: 值} 写入覆盖层；remove 为要删除的参数名列表。落盘为嵌套 YAML。"""
    data = _load_overlay_data()
    changed = False
    for name, val in (values or {}).items():
        path = HOT_PARAMS[name]["yaml"]
        node = data
        for p in path[:-1]:
            node = node.setdefault(p, {})
        node[path[-1]] = val
        changed = True
    for name in (remove or []):
        path = HOT_PARAMS[name]["yaml"]
        node = data
        ok = True
        for p in path[:-1]:
            if not isinstance(node.get(p), dict):
                ok = False
                break
            node = node[p]
        if ok and path[-1] in node:
            del node[path[-1]]
            # 清理空父级
            cur = data
            chain = [data]
            for p in path[:-1]:
                cur = cur[p]
                chain.append(cur)
            for i in range(len(chain) - 1, 0, -1):
                parent, child = chain[i - 1], chain[i]
                key = path[i - 1] if i - 1 < len(path) else None
                if isinstance(parent, dict) and child == {} and key in parent:
                    del parent[key]
            changed = True
    if changed:
        _atomic_write_yaml(OVERLAY_FILE, data)
        # 同步内存中的覆盖层视图，来源徽标无需重启即可更新
        global OVERLAY_LOADED
        OVERLAY_LOADED = data


def _apply_globals(cand, thr):
    """校验通过后写模块全局并同步派生值。"""
    g = globals()
    for k, v in cand.items():
        g[k] = v
    if "TIER_THRESHOLDS" in cand or "MID_MIN_CHARS" in cand or "HEAVY_MIN_CHARS" in cand:
        g["TIER_THRESHOLDS"] = thr
        if len(TIER_ORDER) >= 2:
            g["MID_MIN_CHARS"] = thr[TIER_ORDER[1]]
        if len(TIER_ORDER) >= 3:
            g["HEAVY_MIN_CHARS"] = thr[TIER_ORDER[2]]
    _recompute_derived()


def _recompute_derived():
    """热调后重算派生值（输入上限等）。"""
    global _CTX_AVAILABLE, MAX_INPUT_CHARS
    _CTX_AVAILABLE = front_ctx_available()
    MAX_INPUT_CHARS = _compute_max_input_chars()


def apply_runtime_params(updates):
    """热调参数：updates = {参数名: 值}。

    返回 (applied: dict, error: str | None)。成功时立即生效并持久化到覆盖层。
    注意：本函数必须保持无 await（整段在 _ADMIN_WRITE_LOCK 下同步执行，
    先落盘后改内存——落盘失败时不产生任何状态变更）。
    """
    if not isinstance(updates, dict) or not updates:
        return {}, "请求体必须是非空映射 {参数名: 值}"
    with _ADMIN_WRITE_LOCK:
        cand = {}
        for k, v in updates.items():
            if k not in HOT_PARAMS:
                return {}, (f"未知或不可热调的参数: {k}（可热调: "
                            f"{', '.join(sorted(HOT_PARAMS))}）")
            val, err = _check_hot_value(k, v)
            if err:
                return {}, f"{k}: {err}"
            cand[k] = val
        thr, err = _effective_state(cand)
        if err:
            return {}, err
        # 先按候选值构建落盘内容并写盘；写盘成功后才改内存，避免状态分叉
        persist_values = {}
        for k in cand:
            if k in ("MID_MIN_CHARS", "HEAVY_MIN_CHARS"):
                persist_values.setdefault("TIER_THRESHOLDS", dict(thr))
            else:
                persist_values[k] = copy.deepcopy(cand[k])
        try:
            _persist_overlay(values=persist_values)
        except OSError as exc:
            return {}, f"覆盖层落盘失败（配置未变更）: {exc}"
        _apply_globals(cand, thr)
        return {k: copy.deepcopy(globals()[k]) for k in cand}, None


def reset_runtime_param(name):
    """恢复参数到 import 时的基础值，并从覆盖层移除。返回 (值, 错误或 None)。"""
    if name not in HOT_PARAMS:
        return None, f"未知或不可热调的参数: {name}"
    with _ADMIN_WRITE_LOCK:
        base = copy.deepcopy(HOT_PARAM_BASE[name])
        thr = None
        if name in ("MID_MIN_CHARS", "HEAVY_MIN_CHARS", "TIER_THRESHOLDS"):
            thr = dict(HOT_PARAM_BASE["TIER_THRESHOLDS"])
            try:
                _persist_overlay(remove=[name])
            except OSError as exc:
                return None, f"覆盖层落盘失败（配置未变更）: {exc}"
            _apply_globals({"TIER_THRESHOLDS": thr}, thr)
            # MID/HEAVY 已由 _apply_globals 同步；再叠加自身基础值（与阈值解耦的历史默认）
            if name in ("MID_MIN_CHARS", "HEAVY_MIN_CHARS"):
                globals()[name] = copy.deepcopy(HOT_PARAM_BASE[name])
        else:
            try:
                _persist_overlay(remove=[name])
            except OSError as exc:
                return None, f"覆盖层落盘失败（配置未变更）: {exc}"
            globals()[name] = copy.deepcopy(base)
            _recompute_derived()
        return copy.deepcopy(globals()[name]), None


# ---- 槽位显存可行性：装不下的槽位标记 unavailable，运行时不强行加载 ----------
def _slot_vram(d):
    """返回 (显存估算 MB, 是否为粗略估算)。优先用配置的实测值 vram_mb。"""
    if str(d.get("type", "local")).lower() == "api":
        return 0, False
    if d.get("vram_mb") is not None:
        return int(d["vram_mb"]), False
    return int(int(d.get("weight_mb") or 0) * 1.35 + 512), True


_FRONT_NEED, _FRONT_EST = _slot_vram(_slots_cfg[FRONT_KEY])
_FRONT_SHARES = {}
if FRONT_DEVICES:
    _per = _FRONT_NEED / len(FRONT_DEVICES)
    for _d in FRONT_DEVICES:
        _FRONT_SHARES[int(_d)] = _per


def _slot_fits(key):
    if MODELS_META[key]["type"] == "api":
        return True, "online API（无本地显存占用）", 0, False
    meta = MODELS_META[key]
    need, estimated = _slot_vram(_slots_cfg[key])
    if DEVICE_COUNT == 0:
        return True, "cpu backend (no VRAM check)", need, estimated
    devs = meta["devices"]
    if not devs:
        return True, "no device assigned", need, estimated
    ratios = meta.get("split_ratio") or [1] * len(devs)
    total_ratio = sum(ratios) or len(devs)
    problems = []
    for i, d in enumerate(devs):
        g = next((x for x in SNAP.get("gpus", [])
                  if int(x.get("index", -1)) == int(d)), None)
        if not g:
            problems.append(f"gpu{d} not visible")
            continue
        share = need * ratios[i] / total_ratio
        reserve = RESERVE_SYSTEM_MB / max(1, DEVICE_COUNT)
        front_share = 0 if key == FRONT_KEY else _FRONT_SHARES.get(int(d), 0)
        usable = (g["free_mb"] - reserve - front_share) * (1 - HEADROOM_RATIO)
        if share > usable:
            problems.append(
                f"gpu{d}: need {share:.0f}MB > usable {usable:.0f}MB "
                f"(free {g['free_mb']}MB, front {front_share:.0f}MB)")
    if problems:
        return False, "; ".join(problems), need, estimated
    return True, "ok", need, estimated


VRAM_PLAN = {}
for _key in MODELS_META:
    _ok, _reason, _need, _est = _slot_fits(_key)
    VRAM_PLAN[_key] = {"vram_mb": _need, "estimated": _est,
                       "available": _ok, "reason": _reason}


# ---- api 槽位上游上下文自动探测（v2.5.1）------------------------------------
# 启动时由 ApiBackend 向上游探测真实 context_length（ollama: /api/show），
# 探测结果记录到 SLOT_CTX_EFFECTIVE 并参与 slot_ctx()/预算/413 阈值计算：
#   effective = min(声明值, 探测值)；未声明时 = 探测值（缺省 API_DEFAULT_CTX）。
# 运行期若上游报「上下文超限」，clamp_slot_context() 把生效值对半收缩自适应。
SLOT_CTX_EFFECTIVE = {}


def _declared_ctx(key):
    m = MODELS_META.get(key) or {}
    return int(m.get("context_window") or 0) or None


def record_detected_context(key, detected, source):
    """记录上游探测到的 context_length，并计算生效值（含前台窗口联动）。"""
    if key not in MODELS_META or not detected or int(detected) <= 0:
        return
    with _ADMIN_WRITE_LOCK:
        declared = _declared_ctx(key)
        effective = min(declared, int(detected)) if declared else int(detected)
        effective = max(1024, effective)
        clamped = bool(declared and declared > int(detected))
        SLOT_CTX_EFFECTIVE[key] = {
            "declared": declared, "detected": int(detected),
            "effective": effective, "source": source, "clamped": clamped,
        }
        if key == FRONT_KEY:
            # 前台窗口变化联动：历史预算 / 输入上限 / 413 阈值全部重算
            global FRONT_NUM_CTX
            FRONT_NUM_CTX = effective
            _recompute_derived()


def clamp_slot_context(key, reason=""):
    """运行期自适应：上游报上下文超限时把生效窗口对半收缩（下限 1024）。"""
    with _ADMIN_WRITE_LOCK:
        eff = SLOT_CTX_EFFECTIVE.setdefault(key, {
            "declared": _declared_ctx(key),
            "detected": None,
            "effective": slot_ctx(key) if key in MODELS_META else API_DEFAULT_CTX,
            "source": "declared", "clamped": False})
        old = int(eff["effective"])
        new = max(1024, old // 2)
        eff["effective"] = new
        eff["clamped"] = True
        eff["source"] = (eff.get("source") or "declared") + f" → clamp({reason[:40]})"
        if key == FRONT_KEY:
            global FRONT_NUM_CTX
            FRONT_NUM_CTX = new
            _recompute_derived()
    print(f"[ctx] slot {key} context clamped: {old} -> {new} ({reason[:80]})", flush=True)
    return new


def slot_fits_now(key, force=True):
    """运行期复查：启动后若别的进程占用显存，加载前重新判断，避免 OOM。

    force=False 时复用 GPU 快照 2 秒缓存，供 /chat 的降级判断使用。
    """
    plan = VRAM_PLAN.get(key)
    if plan is None:
        return True, "unknown slot"
    if MODELS_META.get(key, {}).get("type") == "api":
        return True, "online API"
    if not plan.get("available", True):
        return False, str(plan.get("reason"))
    if DEVICE_COUNT == 0:
        return True, "cpu backend"
    meta = MODELS_META[key]
    devs = meta["devices"]
    if not devs:
        return True, "no device assigned"
    ratios = meta.get("split_ratio") or [1] * len(devs)
    total_ratio = sum(ratios) or len(devs)
    snap = gpu.snapshot(GPU_BACKEND_PREF, force=force)
    need = plan["vram_mb"]
    for i, d in enumerate(devs):
        g = next((x for x in snap.get("gpus", [])
                  if int(x.get("index", -1)) == int(d)), None)
        if not g:
            return False, f"gpu{d} not visible"
        share = need * ratios[i] / total_ratio
        reserve = RESERVE_SYSTEM_MB / max(1, DEVICE_COUNT)
        usable = (g["free_mb"] - reserve) * (1 - HEADROOM_RATIO)
        if share > usable:
            return False, (f"gpu{d}: need {share:.0f}MB > usable {usable:.0f}MB "
                           f"(free {g['free_mb']}MB)")
    return True, "ok"


def slot_ctx(key):
    """槽位有效上下文窗口。

    local 槽位 = 全局 NUM_CTX；api 槽位优先用「上游探测后的生效值」
    （SLOT_CTX_EFFECTIVE，启动探测 + 运行期自适应钳制），未探测时
    用声明值 context_window（缺省 API_DEFAULT_CTX）。
    """
    m = MODELS_META.get(key) or {}
    eff = SLOT_CTX_EFFECTIVE.get(key)
    if eff:
        return int(eff["effective"])
    if m.get("type") == "api":
        return int(m.get("context_window") or API_DEFAULT_CTX)
    return NUM_CTX


def slot_max_tokens(key):
    """槽位生成上限：api 槽位可按槽位覆盖，其余用全局 MAX_OUTPUT_TOKENS。"""
    m = MODELS_META.get(key) or {}
    if m.get("type") == "api" and m.get("slot_max_tokens"):
        return int(m["slot_max_tokens"])
    return MAX_OUTPUT_TOKENS


def describe():
    """当前生效配置摘要，供 /health 与运维排查使用。"""
    slots_desc = {}
    for k, v in MODELS_META.items():
        d = {
            "type": v["type"],
            "model": v["model_ref"], "role": v["tier"], "quant": v["quant"],
            "devices": v["devices"], "split_ratio": v["split_ratio"],
            "weight_mb": v["weight_mb"], "resident": v["resident"],
        }
        if v["type"] == "api":
            key_env = v["api_key_env"] or None
            eff = SLOT_CTX_EFFECTIVE.get(k)
            d.update({
                "provider": v.get("provider") or "openai",
                "base_url": v["base_url"],
                "context_window": v["context_window"],
                "context_window_effective": (eff["effective"] if eff
                                             else (v["context_window"]
                                                   or API_DEFAULT_CTX)),
                "context_window_detected": (eff or {}).get("detected"),
                "context_window_source": (eff or {}).get("source")
                                        or ("declared" if v["context_window"]
                                            else f"default({API_DEFAULT_CTX})"),
                "max_output_tokens": v.get("slot_max_tokens"),
                "api_key_source": (f"env:{key_env}" if key_env and os.getenv(key_env)
                                   else ("settings.yaml" if v["api_key"] else None)),
            })
        slots_desc[k] = d
    return {
        "settings_file": str(SETTINGS_FILE),
        "overlay_file": str(OVERLAY_FILE),
        "overlay_loaded": bool(OVERLAY_LOADED),
        "backend": GPU_BACKEND,
        "backend_preference": GPU_BACKEND_PREF,
        "gpu_count": DEVICE_COUNT,
        "total_vram_mb": TOTAL_VRAM_MB,
        "num_ctx": NUM_CTX,
        "num_ctx_source": NUM_CTX_SOURCE,
        "front_type": FRONT_TYPE,
        "front_num_ctx": FRONT_NUM_CTX,
        "kv_cache": KV_CACHE,
        "ports": {"api": API_PORT, "front": FRONT_PORT, "slot": SLOT_PORT},
        "front_slot": FRONT_KEY,
        "tier_order": TIER_ORDER,
        "tier_to_slot": TIER_TO_SLOT,
        "tier_thresholds": TIER_THRESHOLDS,
        "tier_fallbacks": TIER_FALLBACKS,
        "complex_words": COMPLEX_WORDS,
        "tier_complex_words": TIER_COMPLEX_WORDS,
        "limits": {
            "max_input_chars": MAX_INPUT_CHARS,
            "front_max_tokens": FRONT_MAX_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "queue_maxsize": QUEUE_MAXSIZE,
            "history_budget_ratio": HISTORY_BUDGET_RATIO,
            "history_min_chars": HISTORY_MIN_CHARS,
            "history_max_messages": HISTORY_MAX_MESSAGES,
            "context_safety_tokens": CONTEXT_SAFETY_TOKENS,
        },
        "compaction": {
            "enabled": COMPACTION_ENABLED,
            "max_summary_chars": COMPACTION_MAX_SUMMARY_CHARS,
            "min_dropped_chars": COMPACTION_MIN_DROPPED_CHARS,
        },
        "paths": {"data": str(DATA), "logs": str(LOGS)},
        "openai": {
            "default_mode": OPENAI_DEFAULT_MODE,
            "wait_timeout_s": OPENAI_WAIT_TIMEOUT_S,
        },
        "vram_plan": VRAM_PLAN,
        "slots": slots_desc,
    }


# ---- 配置管理（启动期参数的编辑视图，v2.5.1）--------------------------------
# 控制台「配置管理」面板的数据来源：全部字段带中文注释与来源徽标，
# 修改写入 settings.local.yaml 覆盖层，经子进程导入预检后重启生效。
# settings.yaml 的注释与结构不受影响；热调参数（无需重启）走 apply_runtime_params，
# 不在本视图内。

def _yaml_get(d, path):
    node = d
    for p in path:
        if not isinstance(node, dict) or p not in node:
            return None
        node = node[p]
    return node


def _overlay_has(*path):
    return _yaml_get(OVERLAY_LOADED, list(path)) is not None


_ENV_FOR = {  # 启动期参数里存在环境变量覆盖的字段（显示来源用）
    "runtime.server_bin": "LLAMA_SERVER", "runtime.lib_dir": "LLAMA_LIB_DIR",
    "runtime.gpu_backend": "GPU_BACKEND", "runtime.n_gpu_layers": "N_GPU_LAYERS",
    "runtime.queue_maxsize": "QUEUE_MAXSIZE", "runtime.ports.api": "API_PORT",
    "runtime.ports.front": "FRONT_PORT", "runtime.ports.slot": "SLOT_PORT",
    "runtime.startup_timeout_s": "STARTUP_TIMEOUT_S",
    "context.num_ctx": "NUM_CTX", "context.kv_cache": "KV_CACHE",
}

_SETTINGS_FIELDS = [
    # (group, dotted path, type, 中文注释)
    ("runtime", "runtime.server_bin", "str",
     "llama-server（llama.cpp）二进制路径；local 槽位用，路径错会导致启动失败"),
    ("runtime", "runtime.lib_dir", "str",
     "llama.cpp 动态库目录（LD_LIBRARY_PATH），可留空"),
    ("runtime", "runtime.gpu_backend", "str",
     "auto | cuda | rocm | cpu；auto 由 nvidia-smi/rocm-smi 探测"),
    ("runtime", "runtime.n_gpu_layers", "int",
     "卸载到 GPU 的层数，99 = 全部卸载；CPU 或部分卸载可调小"),
    ("runtime", "runtime.queue_maxsize", "int",
     "后台任务排队上限；满时 /chat 返回 429，0 表示不限制"),
    ("runtime", "runtime.ports.api", "int",
     "API 服务端口（/chat /v1/* /console）"),
    ("runtime", "runtime.ports.front", "int",
     "本地前台 llama-server 端口（api 前台不使用）"),
    ("runtime", "runtime.ports.slot", "int",
     "本地后台槽位共享端口（api 槽位不使用）"),
    ("runtime", "runtime.startup_timeout_s", "int",
     "启动等待模型就绪的超时秒数；大模型/慢盘可调大"),
    ("context", "context.num_ctx", "str",
     "上下文窗口：auto 按显存查 auto_table，或直接写整数；最低 32K，运行期常量"),
    ("context", "context.kv_cache", "str",
     "KV cache 精度：f16 / q8_0 等；只省显存，不改变 num_ctx"),
    ("context", "context.auto_table", "list",
     "auto 模式的查表：[{min_total_mb: 总显存MB, num_ctx: 档位}]，从大到小取第一条命中"),
    ("embedding", "embedding.onnx", "str",
     "ONNX 嵌入模型路径（语义线索用，CPU 执行不占显存）；加载失败为非致命降级"),
    ("embedding", "embedding.tokenizer", "str",
     "嵌入模型 tokenizer.json 路径"),
    ("budget", "budget.reserve_system_mb", "int",
     "系统/驱动显存预留（按可见卡数分摊）；装不下的槽位标记不可用"),
    ("budget", "budget.headroom_ratio", "float",
     "显存安全余量比例（0.10 = 10%），宁降级不 OOM"),
    ("routing", "routing.tier_order", "list",
     "档位名列表：第 1 个=直接回答，其余按 tier_thresholds 从高到低分级；可 3+ 档"),
    ("routing", "routing.tier_to_slot", "dict",
     "档位 → 槽位 key 映射；每个档位都必须有映射，指向不存在的槽位启动失败"),
    ("routing", "routing.tier_fallbacks", "dict",
     "可选：每档降级链（如 heavy: [moe, cloud]）；映射槽位不可用时按序尝试"),
    ("routing", "routing.max_input_chars", "str",
     "单次输入字符上限：auto = 前台窗口推导，或显式整数；超限返回 413"),
]

_SLOT_COMMON = [
    ("type", "str", "local（本地 GGUF + llama-server 进程）| api（OpenAI 兼容线上模型）"),
    ("role", "str", "档位标签（显示用）"),
    ("resident", "bool", "前台常驻槽位标记；全文件必须有且仅有一个 true"),
]
_SLOT_LOCAL = [
    ("model", "str", "GGUF 文件路径（相对工作区根目录）"),
    ("quant", "str", "量化标签（显示用）"),
    ("weight_mb", "int", "权重大小估算 MB；参与多卡分层比例计算"),
    ("vram_mb", "int", "权重 + KV 的实测显存 MB；显存可行性判断依据，务必实测校准"),
    ("placement", "str", "auto | spread（多卡分层）| explicit（devices 指定）"),
    ("split_ratio", "list", "可选：多卡分层比例，如 [1, 2]；不写按空闲显存自动算"),
]
_SLOT_API = [
    ("provider", "str", "openai（OpenAI 兼容 /v1，默认）| ollama（本机 Ollama 原生 /api/chat，thinking 模型自动 think:false）"),
    ("base_url", "str", "API 根地址：openai 必须含版本段（如 https://api.deepseek.com/v1）；ollama 填 http://127.0.0.1:11434"),
    ("model", "str", "远端模型名，如 deepseek-chat / qwen3.5:4b"),
    ("extra_body", "dict", "可选：附加到每次请求体的字段（如 {\"think\": true}；ollama 槽位默认已带 think:false）"),
    ("api_key", "str", "明文密钥（仅显示掩码 ***；输入新值覆盖、留空=清除；推荐留空走 api_key_env）"),
    ("api_key_env", "str", "密钥环境变量名，优先于 api_key；密钥不出现在日志与 /config"),
    ("context_window", "int", "远端模型上下文窗口（tokens）；**留空 = 启动时自动探测上游**（ollama /api/show，声明值与探测值取 min 生效）"),
    ("max_output_tokens", "int", "可选：该槽位生成上限，缺省用全局值"),
]


def settings_schema():
    """全量启动期配置编辑视图（含槽位动态字段）。"""
    fields = []
    for group, path, ftype, desc in _SETTINGS_FIELDS:
        env_name = _ENV_FOR.get(path)
        if env_name and os.getenv(env_name) not in (None, ""):
            source = "env"
        elif _overlay_has(*path.split(".")):
            source = "overlay"
        else:
            source = "base"
        fields.append({"path": path, "group": group, "type": ftype,
                       "desc": desc, "value": _yaml_get(_S, path.split(".")),
                       "source": source, "restart": True,
                       "overlay_value": _yaml_get(OVERLAY_LOADED, path.split("."))})
    for key, meta in MODELS_META.items():
        for name, ftype, desc in _SLOT_COMMON:
            p = f"slots.{key}.{name}"
            fields.append({
                "path": p, "group": "slots", "type": ftype, "desc": desc,
                "value": _yaml_get(_S, ["slots", key, name]),
                "source": "overlay" if _overlay_has("slots", key, name) else "base",
                "overlay_value": _yaml_get(OVERLAY_LOADED, ["slots", key, name]),
                "restart": True})
        extra = _SLOT_API if meta["type"] == "api" else _SLOT_LOCAL
        for name, ftype, desc in extra:
            p = f"slots.{key}.{name}"
            if name == "model" and meta["type"] == "api":
                desc += "（api 槽位填远端模型名）"
            raw = _yaml_get(_S, ["slots", key, name])
            # 明文密钥只出掩码：值本身绝不进入接口响应与控制台 HTML
            value = ("***" if raw else "") if name == "api_key" else raw
            fields.append({
                "path": p, "group": "slots", "type": ftype, "desc": desc,
                "value": value,
                "source": "overlay" if _overlay_has("slots", key, name) else "base",
                "overlay_value": (("***" if _yaml_get(OVERLAY_LOADED,
                                                      ["slots", key, name]) else "")
                                  if name == "api_key"
                                  else _yaml_get(OVERLAY_LOADED, ["slots", key, name])),
                "restart": True})
    return {
        "overlay_file": str(OVERLAY_FILE),
        "fields": fields,
        "slots": list(MODELS_META.keys()),
        "front_slot": FRONT_KEY,
        "note": ("这里的修改写入 settings.local.yaml 覆盖层（settings.yaml 注释不受影响），"
                 "保存时自动做启动预检，重启服务后生效；"
                 "阈值/词表/输出上限等可热调参数请在「参数调整」面板修改。"),
    }


def _path_is_safe(path):
    """dotted path 合法性：段名只允许标识符字符，防止路径注入到任意 YAML 结构。"""
    if not isinstance(path, str) or not path:
        return False
    return all(seg and _ident_ok(seg) for seg in path.split("."))


def _ident_ok(seg):
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]+", seg))


def build_candidate_overlay(updates, resets, removes):
    """按 dotted path 把更新/移除/删除落到覆盖层副本上。

    updates: {path: value} —— 写覆盖值；
    resets:  [path] —— 仅移除覆盖值，恢复 settings.yaml 原值（无覆盖时报错）；
    removes: [path] —— 彻底删除（含 settings.yaml 来源，经 __remove__）。
    返回 (candidate dict, error 或 None)。
    """
    candidate = copy.deepcopy(OVERLAY_LOADED)

    def _anchor(parts):
        node = candidate
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = node.setdefault(p, {})
            node = nxt
        return node

    for path in (resets or []):
        if not _path_is_safe(path):
            return None, f"非法配置路径: {path}"
        parts = path.split(".")
        node = _yaml_get(candidate, parts[:-1])
        if not isinstance(node, dict) or parts[-1] not in node:
            return None, f"{path} 没有覆盖层值可移除（该字段来自 settings.yaml）"
        del node[parts[-1]]
    for path in (removes or []):
        if not _path_is_safe(path):
            return None, f"非法配置路径: {path}"
        parts = path.split(".")
        anchor = _anchor(parts)
        anchor.pop(parts[-1], None)          # 覆盖层里存在的：先删
        lst = anchor.setdefault("__remove__", [])
        if parts[-1] not in lst:
            lst.append(parts[-1])            # 合并时连 settings.yaml 同名键一起删除
    for path, value in (updates or {}).items():
        if not _path_is_safe(path):
            return None, f"非法配置路径: {path}"
        parts = path.split(".")
        node = candidate
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    _prune_shadowed(candidate)
    return candidate, None


def _prune_shadowed(node):
    """__remove__ 里登记的键，同节点不允许再保留覆盖值（删除优先）。"""
    if not isinstance(node, dict):
        return
    remove = node.get("__remove__") or []
    for k in remove:
        node.pop(k, None)
    for v in list(node.values()):
        if isinstance(v, dict):
            _prune_shadowed(v)


def validate_overlay(candidate):
    """用候选覆盖层做一次真实的配置导入预检（子进程），返回 (ok, error)。

    同步阻塞调用：async 侧必须经 asyncio.to_thread 调用（见 main.py）。
    tmp 文件名带唯一后缀，避免并发预检互相覆盖。
    """
    tmp = DATA / f".overlay-validate-{uuid.uuid4().hex[:8]}.yaml"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    env = os.environ.copy()
    env["SETTINGS_LOCAL_FILE"] = str(tmp)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "pylib")]) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        r = subprocess.run([sys.executable, "-c", "import app.config"],
                           env=env, capture_output=True, text=True, timeout=180)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if r.returncode != 0:
        lines = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
        return False, (lines[-1][:500] if lines else "配置预检失败（无输出）")
    return True, ""


def commit_overlay(candidate):
    """预检通过后落盘候选覆盖层，并同步内存视图（原子写 + 串行化）。"""
    with _ADMIN_WRITE_LOCK:
        _atomic_write_yaml(OVERLAY_FILE, candidate)
        global OVERLAY_LOADED
        OVERLAY_LOADED = copy.deepcopy(candidate)

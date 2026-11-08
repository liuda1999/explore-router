"""explore 配置入口。

唯一真源是工作区根目录的 settings.yaml：
- slots 是「角色槽位」，模型文件 / 量化 / 显存预算 / 设备分配全部来自配置；
- GPU 数量、品牌、后端由 gpu.snapshot() 运行时探测，代码不假设双卡或 NVIDIA；
- 环境变量仅作部署期覆盖，优先级最高，便于同一份配置跑在不同设备上。

改模型 / 换设备只改 settings.yaml，不改本文件。
"""
import os
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
_slots_cfg = _S.get("slots", {}) or {}
if not _slots_cfg:
    _fail("slots 不能为空")


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
    _devs = _base_devices(_d)
    MODELS_META[_key] = {
        "path": ROOT / _d["model"],
        "model_ref": _d["model"],
        "tier": _d.get("role", _key),
        "devices": _devs,
        "devices_str": ",".join(str(x) for x in _devs),
        "split_ratio": None,
        "weight_mb": _d.get("weight_mb"),
        "vram_mb": _d.get("vram_mb"),
        "quant": _d.get("quant", ""),
        "resident": bool(_d.get("resident", False)),
    }

_residents = [k for k, v in MODELS_META.items() if v["resident"]]
if len(_residents) != 1:
    _fail(f"resident: true 的槽位必须恰好一个，当前 {_residents or '无'}")
FRONT_KEY = _residents[0]
FRONT_MODEL = MODELS_META[FRONT_KEY]["path"]
BACKEND_SLOTS = [k for k in MODELS_META if k != FRONT_KEY]
FRONT_DEVICES = MODELS_META[FRONT_KEY]["devices"]
FRONT_DEVICE = FRONT_DEVICES[0] if FRONT_DEVICES else None
FRONT_WEIGHT_MB = int(MODELS_META[FRONT_KEY].get("weight_mb") or 0)


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


# 第二遍：多卡槽位计算分层比例
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
MID_MIN_CHARS = int(_routing.get("mid_min_chars", 25))
HEAVY_MIN_CHARS = int(_routing.get("heavy_min_chars", 120))
COMPLEX_WORDS = list(_routing.get("complex_words", []) or [])
FRONT_SYSTEM = (_S.get("frontend", {}) or {}).get("system_prompt", "你是本地助手，请简洁回应。")
TIER_ORDER = [str(x) for x in (_routing.get("tier_order") or ["simple", "mid", "heavy"])]

_emb = _S.get("embedding", {}) or {}
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

# 历史预算按 num_ctx 自动计算：不再用固定条数，小上下文自动收紧
HISTORY_BUDGET_RATIO = float(_front.get("history_budget_ratio", 0.45))
HISTORY_MIN_CHARS = int(_front.get("history_min_chars", 2048))
HISTORY_MAX_MESSAGES = int(_front.get("history_max_messages", 40))
CONTEXT_SAFETY_TOKENS = int(_front.get("context_safety_tokens", 256))

# 按 1 字符≈1 token 保守估算，为输出、system、安全余量、最小历史预留
_CTX_AVAILABLE = NUM_CTX - FRONT_MAX_TOKENS - CONTEXT_SAFETY_TOKENS - len(FRONT_SYSTEM)
if _CTX_AVAILABLE <= 0:
    _fail(f"num_ctx={NUM_CTX} 太小：扣除输出/安全余量/system 后无可用空间")

_max_chars = _routing.get("max_input_chars")
if _max_chars in (None, 0, "auto"):
    # 输入上限 = 可用上下文 - 最小历史预算；保证任何输入都不会挤掉输出与安全余量
    MAX_INPUT_CHARS = max(512, _CTX_AVAILABLE - HISTORY_MIN_CHARS)
else:
    MAX_INPUT_CHARS = int(_max_chars)


# ---- 启动期配置校验：错配在启动时报清楚，而不是运行中静默降级 -----------------
def _validate():
    if len(TIER_ORDER) < 3:
        _fail(f"routing.tier_order 至少需要 3 个档位（simple/mid/heavy 语义），当前 {TIER_ORDER}")
    if len(set(TIER_ORDER)) != len(TIER_ORDER):
        _fail(f"routing.tier_order 有重复档位: {TIER_ORDER}")
    missing = [t for t in TIER_ORDER[:3] if t not in TIER_TO_SLOT]
    if missing:
        _fail(f"routing.tier_to_slot 缺少档位映射: {missing}（当前 {sorted(TIER_TO_SLOT)}）")
    bad_slots = {t: s for t, s in TIER_TO_SLOT.items() if s not in MODELS_META}
    if bad_slots:
        _fail(f"routing.tier_to_slot 指向不存在的槽位: {bad_slots}")
    if DEFAULT_SLOT not in MODELS_META:
        _fail(f"routing.default_slot 不存在: {DEFAULT_SLOT}")
    if not (0 < MID_MIN_CHARS < HEAVY_MIN_CHARS):
        _fail(f"分级阈值需满足 0 < mid_min_chars({MID_MIN_CHARS}) < "
              f"heavy_min_chars({HEAVY_MIN_CHARS})")
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
    if MAX_INPUT_CHARS < 128 or reserved > NUM_CTX:
        _fail(f"上下文预算超出 num_ctx={NUM_CTX}: 输入上限 {MAX_INPUT_CHARS} + "
              f"最小历史 {HISTORY_MIN_CHARS} + 输出 {FRONT_MAX_TOKENS} + "
              f"安全余量 {CONTEXT_SAFETY_TOKENS} + system {len(FRONT_SYSTEM)} "
              f"= {reserved}")
    for k, v in MODELS_META.items():
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


# ---- 槽位显存可行性：装不下的槽位标记 unavailable，运行时不强行加载 ----------
def _slot_vram(d):
    """返回 (显存估算 MB, 是否为粗略估算)。优先用配置的实测值 vram_mb。"""
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


def slot_fits_now(key, force=True):
    """运行期复查：启动后若别的进程占用显存，加载前重新判断，避免 OOM。

    force=False 时复用 GPU 快照 2 秒缓存，供 /chat 的降级判断使用。
    """
    plan = VRAM_PLAN.get(key)
    if plan is None:
        return True, "unknown slot"
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


def describe():
    """当前生效配置摘要，供 /health 与运维排查使用。"""
    return {
        "settings_file": str(SETTINGS_FILE),
        "backend": GPU_BACKEND,
        "backend_preference": GPU_BACKEND_PREF,
        "gpu_count": DEVICE_COUNT,
        "total_vram_mb": TOTAL_VRAM_MB,
        "num_ctx": NUM_CTX,
        "num_ctx_source": NUM_CTX_SOURCE,
        "kv_cache": KV_CACHE,
        "ports": {"api": API_PORT, "front": FRONT_PORT, "slot": SLOT_PORT},
        "front_slot": FRONT_KEY,
        "tier_order": TIER_ORDER,
        "tier_to_slot": TIER_TO_SLOT,
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
        "paths": {"data": str(DATA), "logs": str(LOGS)},
        "openai": {
            "default_mode": OPENAI_DEFAULT_MODE,
            "wait_timeout_s": OPENAI_WAIT_TIMEOUT_S,
        },
        "vram_plan": VRAM_PLAN,
        "slots": {
            k: {
                "model": v["model_ref"], "role": v["tier"], "quant": v["quant"],
                "devices": v["devices"], "split_ratio": v["split_ratio"],
                "weight_mb": v["weight_mb"], "resident": v["resident"],
            }
            for k, v in MODELS_META.items()
        },
    }

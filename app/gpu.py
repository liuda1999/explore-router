"""GPU 后端抽象：CUDA / ROCm / CPU。

只负责探测，不假设卡数、品牌或型号；分配策略在 config.py 里由 settings.yaml 决定。
探测失败时显式标注，不用 0 显存静默继续。
"""
import os
import re
import shutil
import subprocess
import threading
import time

_CACHE_TTL_S = 2.0
_CACHE = {"ts": 0.0, "prefer": None, "snap": None}
_CACHE_LOCK = threading.Lock()


def _empty(backend, error=None):
    d = {"backend": backend, "gpus": [], "total_mb": 0, "used_mb": 0, "free_mb": 0}
    if error:
        d["error"] = error
    return d


def _nvidia():
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,memory.total,memory.used,memory.free,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        )
        if out.returncode != 0:
            return _empty("cuda", out.stderr.strip()[:200])
        gpus = []
        for line in out.stdout.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 6:
                continue
            gpus.append({
                "index": int(p[0]), "name": p[1],
                "total_mb": int(float(p[2])), "used_mb": int(float(p[3])),
                "free_mb": int(float(p[4])), "driver": p[5],
            })
        if not gpus:
            return _empty("cuda", "no GPUs parsed")
        return {
            "backend": "cuda", "gpus": gpus,
            "total_mb": sum(g["total_mb"] for g in gpus),
            "used_mb": sum(g["used_mb"] for g in gpus),
            "free_mb": sum(g["free_mb"] for g in gpus),
        }
    except Exception as exc:
        return _empty("cuda", str(exc))


def _rocm():
    if not shutil.which("rocm-smi"):
        return None
    try:
        out = subprocess.run(["rocm-smi", "--showmeminfo", "vram"],
                             capture_output=True, text=True, timeout=8)
        text = out.stdout
        # 新版 rocm-smi 带 GPU[i] 前缀，按卡解析；老版本只有总量，退化为单卡合计
        total_map = {int(i): int(v) for i, v in re.findall(
            r"GPU\[(\d+)\]\s*:\s*VRAM Total Memory \(B\):\s*(\d+)", text)}
        used_map = {int(i): int(v) for i, v in re.findall(
            r"GPU\[(\d+)\]\s*:\s*VRAM Total Used Memory \(B\):\s*(\d+)", text)}
        if not total_map:
            totals = re.findall(r"VRAM Total Memory \(B\): (\d+)", text)
            useds = re.findall(r"VRAM Total Used Memory \(B\): (\d+)", text)
            if not totals:
                return _empty("rocm", out.stderr.strip()[:200] or "no VRAM info")
            total_map = {i: int(v) for i, v in enumerate(totals)}
            used_map = {i: int(v) for i, v in enumerate(useds)}
        gpus = []
        for idx in sorted(total_map):
            total = total_map[idx] // (1024 * 1024)
            used = used_map.get(idx, 0) // (1024 * 1024)
            gpus.append({
                "index": idx, "name": "AMD GPU",
                "total_mb": total, "used_mb": used,
                "free_mb": max(0, total - used), "driver": "",
            })
        t = sum(g["total_mb"] for g in gpus)
        u = sum(g["used_mb"] for g in gpus)
        return {"backend": "rocm", "gpus": gpus, "total_mb": t,
                "used_mb": u, "free_mb": max(0, t - u)}
    except Exception as exc:
        return _empty("rocm", str(exc))


def _snapshot_uncached(prefer):
    if prefer == "cpu":
        return _empty("cpu", "forced cpu backend")
    if prefer == "cuda":
        return _nvidia() or _empty("cuda", "nvidia-smi unavailable")
    if prefer == "rocm":
        return _rocm() or _empty("rocm", "rocm-smi unavailable")
    return _nvidia() or _rocm() or _empty("cpu", "no GPU backend detected")


def _apply_visible(snap):
    """尊重 CUDA_VISIBLE_DEVICES / HIP_VISIBLE_DEVICES，按可见设备重新编号。

    nvidia-smi 会列出整机所有卡，但容器/部署常只挂一张。配置和槽位分配
    必须按“进程实际可见的卡”来算，否则单卡设备会按双卡规划显存。
    """
    vis = os.getenv("CUDA_VISIBLE_DEVICES") or os.getenv("HIP_VISIBLE_DEVICES")
    if not vis or not snap.get("gpus"):
        return snap
    items = [x.strip() for x in vis.split(",") if x.strip()]
    if not items or any(not x.isdigit() for x in items):
        return snap          # UUID / 复杂写法不处理，按整机可见
    idxs = [int(x) for x in items]
    if any(i < 0 for i in idxs):
        return _empty(snap.get("backend", "cuda"), "GPUs disabled by visibility env")
    gpus = [g for g in snap["gpus"] if int(g.get("index", -1)) in idxs]
    if not gpus:
        return snap
    for new_i, g in enumerate(gpus):
        g["physical_index"] = int(g.get("physical_index", g["index"]))
        g["index"] = new_i
    total = sum(g["total_mb"] for g in gpus)
    used = sum(g["used_mb"] for g in gpus)
    return {**snap, "gpus": gpus, "total_mb": total, "used_mb": used,
            "free_mb": max(0, total - used)}


def snapshot(prefer=None, force=False):
    """prefer: auto | cuda | rocm | cpu；auto 时按可用性探测。

    带 2 秒缓存：/health 可能被频繁轮询，避免每次都同步调用 nvidia-smi。
    """
    p = (prefer or "auto").lower()
    now = time.time()
    with _CACHE_LOCK:
        if (not force and _CACHE["snap"] is not None
                and _CACHE["prefer"] == p and now - _CACHE["ts"] < _CACHE_TTL_S):
            return _CACHE["snap"]
    snap = _apply_visible(_snapshot_uncached(p))
    with _CACHE_LOCK:
        _CACHE.update(ts=now, prefer=p, snap=snap)
    return snap


def split_ratio_for(snap, devices):
    """按各卡空闲显存生成分层比例；单卡返回 None（不启用多卡分层）。

    比例放大 10 倍取整：直接用 round(free/base) 会把 1.3 压成 1，
    退化为均分，浪费空闲更多那张卡的显存。10 倍精度足以表达 0.1 的差异，
    同时保持数值很小，`-ts` 归一化后与 MB 比例等价。
    """
    gpus = snap.get("gpus", [])
    if len(devices) < 2:
        return None
    by_index = {int(g.get("index", i)): g for i, g in enumerate(gpus)}
    free = []
    for i in devices:
        g = by_index.get(int(i)) or (gpus[0] if gpus else {})
        free.append(max(1, int(g.get("free_mb", 1))))
    base = min(free)
    return [max(1, round(f / base * 10)) for f in free]

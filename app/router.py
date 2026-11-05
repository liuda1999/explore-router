"""分级决策与后台互斥槽位。

槽位数量、模型、设备、档位名称全部来自 settings.yaml；本模块只按 key 操作，
不假设有几个槽位、也不假设各槽位装的是什么模型。
单队列顺序执行 -> 同一时刻只有一个后台任务 -> 槽位天然互斥。
"""
import asyncio

from . import config
from .llama import LlamaServer


def classify(text, prefer=None):
    """返回配置里的档位名（默认 simple / mid / heavy）。

    档位名来自 routing.tier_order，代码不写死；三档语义固定为
    「直接回答 / 常规后台 / 重档后台」，阈值与复杂词来自配置。
    """
    if prefer in config.TIER_ORDER:
        return prefer, f"explicit prefer_tier={prefer}"
    n = len(text.strip())
    hits = [w for w in config.COMPLEX_WORDS if w.lower() in text.lower()]
    simple_tier, mid_tier, heavy_tier = config.TIER_ORDER[:3]
    if n >= config.HEAVY_MIN_CHARS or len(hits) >= 2:
        return heavy_tier, f"len={n} >= {config.HEAVY_MIN_CHARS} or complex words={hits[:3]}"
    if n >= config.MID_MIN_CHARS or hits:
        return mid_tier, f"len={n} >= {config.MID_MIN_CHARS} or complex words={hits[:3]}"
    return simple_tier, f"short input len={n}"


def _slot_available(key):
    """前台已常驻，始终可直答；后台槽位按静态显存规划判断。

    注意：不能在切换前用实时 free 判断目标槽位——当前驻留的后台模型
    会在切换时先被停掉，把它算进占用会误判“装不下”。
    """
    if key == config.FRONT_KEY:
        return True
    return config.VRAM_PLAN.get(key, {}).get("available", True)


def slot_for_tier(tier):
    """tier -> 槽位 key；目标槽位显存不足时自动降级到可用槽位。"""
    target = config.TIER_TO_SLOT.get(tier) or config.DEFAULT_SLOT
    if _slot_available(target):
        return target
    for cand in (config.DEFAULT_SLOT, config.FRONT_KEY):
        if _slot_available(cand):
            return cand
    return target


class SlotManager:
    def __init__(self):
        self.current = None
        self.server = None
        self.lock = asyncio.Lock()
        self.switches = 0
        self.last_switch_reason = ""
        self.last_error = ""

    async def ensure(self, slot_key):
        """确保槽位里是指定模型；需要切换时才停旧启新。"""
        async with self.lock:
            if self.current == slot_key and self.server and await self.server.healthy():
                return self.server, False
            # 先校验目标槽位，避免配置写错时把正在工作的旧模型停掉
            meta = config.MODELS_META.get(slot_key)
            if not meta:
                self.last_error = f"unknown slot in settings.yaml: {slot_key}"
                raise RuntimeError(self.last_error)
            reason = f"switch {self.current or 'idle'} -> {slot_key}"
            if self.server:
                await self.server.stop()
            candidate = LlamaServer(
                slot_key, config.SLOT_PORT, meta["path"], meta["devices"],
                split_ratio=meta["split_ratio"],
            )
            try:
                await candidate.start()
            except Exception as exc:
                self.server = None
                self.current = None
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise
            self.server = candidate
            self.current = slot_key
            self.switches += 1
            self.last_switch_reason = reason
            self.last_error = ""
            return self.server, True

    async def stop(self):
        async with self.lock:
            if self.server:
                await self.server.stop()
            self.server = None
            self.current = None

    def describe(self):
        return {"current": self.current, "switches": self.switches,
                "last_switch_reason": self.last_switch_reason,
                "last_error": self.last_error,
                "load_ms": self.server.load_ms if self.server else None}

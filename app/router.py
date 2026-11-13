"""分级决策与后台互斥槽位。

槽位数量、模型、设备、档位名称全部来自 settings.yaml；本模块只按 key 操作，
不假设有几个槽位、也不假设各槽位装的是什么模型（local 进程或线上 API）。
单队列顺序执行 -> 同一时刻只有一个后台任务 -> 本地槽位天然互斥；
type: api 槽位无本地进程，不参与互斥驻留，经缓存客户端直接调用。
"""
import asyncio

from . import config
from .api_backend import create_backend
from .llama import LlamaServer


def classify(text, prefer=None):
    """返回配置里的档位名（默认 simple / mid / heavy，可扩展更多档）。

    档位名来自 routing.tier_order，代码不写死。语义：
    - 第 1 档 = 直接回答；**强意图信号优先**：全局复杂词命中 >= 2 直接归
      重档（沿用 v2.4 语义——「请详细分析…」这类短句不该被长度阈值截胡）；
    - 阈值（routing.tier_thresholds）与档位专属强词表（tier_complex_words）
      从最高档往下判；单个复杂词命中归常规后台档。
    """
    if prefer in config.TIER_ORDER:
        return prefer, f"explicit prefer_tier={prefer}"
    n = len(text.strip())
    low = text.lower()
    order = config.TIER_ORDER
    hits = [w for w in config.COMPLEX_WORDS if w.lower() in low]
    # 1) 强意图信号优先：全局复杂词 >=2 命中 -> 重档
    if len(hits) >= 2:
        return order[2], f"complex words={hits[:3]}"
    # 2) 阈值与档位专属词表：从最高档往下，首个命中即归档
    for tier in reversed(order[1:]):
        thr = config.TIER_THRESHOLDS.get(tier)
        if thr is not None and n >= thr:
            return tier, f"len={n} >= {thr}"
        t_words = config.TIER_COMPLEX_WORDS.get(tier) or []
        t_hits = [w for w in t_words if w.lower() in low]
        if t_hits:
            return tier, f"tier words={t_hits[:3]}"
    # 3) 单个全局复杂词 -> 常规后台档
    if hits:
        return order[1], f"complex words={hits[:3]}"
    return order[0], f"short input len={n}"


def _slot_available(key):
    """前台已常驻，始终可直答；线上 API 槽位无显存约束恒可用；
    本地后台槽位按静态显存规划判断。

    注意：不能在切换前用实时 free 判断目标槽位——当前驻留的后台模型
    会在切换时先被停掉，把它算进占用会误判“装不下”。
    """
    if key == config.FRONT_KEY:
        return True
    if (config.MODELS_META.get(key) or {}).get("type") == "api":
        return True
    return config.VRAM_PLAN.get(key, {}).get("available", True)


def slot_for_tier(tier):
    """tier -> 槽位 key；映射槽位不可用时依次尝试：
    tier_fallbacks 列表 -> default_slot -> 前台。
    """
    target = config.TIER_TO_SLOT.get(tier) or config.DEFAULT_SLOT
    if _slot_available(target):
        return target
    for cand in config.TIER_FALLBACKS.get(tier) or []:
        if _slot_available(cand):
            return cand
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
        self.api_clients = {}   # slot_key -> ApiBackend（无进程，缓存复用）

    async def ensure(self, slot_key):
        """确保槽位里是指定后端；本地槽位需要切换时才停旧启新。

        返回 (backend, switched)。api 槽位恒返回 (client, False)。
        """
        async with self.lock:
            # 先校验目标槽位，避免配置写错时把正在工作的旧模型停掉
            meta = config.MODELS_META.get(slot_key)
            if not meta:
                self.last_error = f"unknown slot in settings.yaml: {slot_key}"
                raise RuntimeError(self.last_error)
            if meta.get("type") == "api":
                client = self.api_clients.get(slot_key)
                if client is None:
                    client = create_backend(slot_key, meta, config.SLOT_PORT)
                    await client.start()
                    self.api_clients[slot_key] = client
                self.last_error = ""
                return client, False
            if self.current == slot_key and self.server and await self.server.healthy():
                return self.server, False
            reason = f"switch {self.current or 'idle'} -> {slot_key}"
            if self.server:
                await self.server.stop()
            candidate = LlamaServer(
                slot_key, config.SLOT_PORT, meta["path"], meta["devices"],
                split_ratio=meta["split_ratio"],
            )
            try:
                await candidate.start()
            except asyncio.CancelledError:
                # 关机/重启取消：已 spawn 未就绪的进程必须回收（llama.start
                # 内部已 stop，这里兜底确保候选状态清空），否则成孤儿占端口
                self.server = None
                self.current = None
                raise
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
            for client in self.api_clients.values():
                try:
                    await client.stop()
                except Exception:
                    pass
            self.api_clients.clear()

    def describe(self):
        return {"current": self.current, "switches": self.switches,
                "last_switch_reason": self.last_switch_reason,
                "last_error": self.last_error,
                "load_ms": self.server.load_ms if self.server else None,
                "api_slots_active": sorted(self.api_clients)}

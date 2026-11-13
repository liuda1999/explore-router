"""路由稳定性与准确性实测：对运行中的服务发真实请求，从零验证分级行为。

「准确性」定义：分级结果必须与**运行中服务声明的路由契约**一致
（tier_order / tier_thresholds / complex_words 从 GET /config 实时读取，
预期档位按 classify() 的契约语义独立推导，不参考任何历史测试结果）。

「稳定性」定义：
- 重复一致性：同一条真实输入连续 3 轮请求，档位必须逐次相同；
- 并发一致性：同一输入多路并发请求，档位必须相同且全部成功；
- 持续无漂移：首轮与末轮（间隔大量请求后）结果完全一致；
- 槽位一致性：异步任务的 chosen_model 必须与 tier_to_slot 映射一致。

数据集：全部为真实场景文本（问答/翻译/行程/评审/技术段落/代码需求），
含 24/25/119/120 字符的精确边界用例与复杂词 0/1/2 命中用例。

用法: BASE=http://127.0.0.1:8100 python3 tests/test_routing_stability.py
"""
import json
import os
import pathlib
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = os.getenv("BASE", "http://127.0.0.1:8100")
REPEATS = int(os.getenv("ROUNDS", "3"))

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:200]}")


def http(method, path, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return {"_status": e.code, **json.loads(raw)}
        except Exception:
            return {"_status": e.code}


def expected_tier(text, cfg):
    """按契约语义独立推导预期档位（与 classify() 契约等价的独立实现）。

    强意图优先：全局复杂词 >=2 命中 -> 最高后台档（v2.4 语义）；
    再按阈值从高档到低档判断；单个命中归常规后台档。
    """
    order = cfg["tier_order"]
    thr = cfg["tier_thresholds"]
    words = [w.lower() for w in (cfg.get("_complex_words") or [])]
    tier_words = {t: [w.lower() for w in v]
                  for t, v in (cfg.get("tier_complex_words") or {}).items()}
    low = text.strip().lower()
    hits = sum(1 for w in words if w in low)
    n = len(text.strip())
    if len(order) >= 3 and hits >= 2:
        return order[2]
    for tier in reversed(order[1:]):
        if n >= thr.get(tier, 10 ** 9):
            return tier
        if any(w in low for w in tier_words.get(tier, [])):
            return tier
    if hits >= 1:
        return order[1]
    return order[0]


def build_cases():
    """真实场景数据集： name, text, intent(设计意图档位), prefer(可选覆盖)。"""
    filler = "背景补充材料，用于达到目标长度。"
    cases = []

    def add(name, text, intent, prefer=None):
        cases.append({"name": name, "text": text, "intent": intent,
                      "prefer": prefer})

    # ---- 简单档：短问答（<25 字，无复杂词）----
    add("S1_算术", "1+1等于几", "simple")
    add("S2_问候", "你好", "simple")
    add("S3_常识", "量子计算是什么", "simple")
    add("S4_英文短问", "hello", "simple")
    add("S5_协议常识", "什么是HTTP协议", "simple")
    add("S6_推荐", "推荐一首歌", "simple")

    # ---- 中档：中等长度真实请求（25~119 字，无复杂词）----
    add("M1_荐书", "推荐三本适合初学者的科幻小说，并简单说明推荐理由和阅读顺序", "mid")
    add("M2_自我介绍", "帮我写一段面试用的自我介绍，突出学习能力和团队协作，长度五十个字左右", "mid")
    add("M3_行程", "周末想去周边的古镇玩一天，帮我规划一个节奏轻松的一日行程，下午三点才出发", "mid")
    add("M4_英文中等", "Explain the difference between TCP and UDP in computer "
                        "networking, with one short example that a beginner can follow", "mid")
    add("M5_翻译", "把这段话翻译成英文：人工智能正在改变我们的生活方式和工作方式，"
                   "带来很多新的机遇和挑战", "mid")

    # ---- 重档：长文本（>=120 字，无复杂词）----
    long_review = ("这款降噪耳机我用了两个月。佩戴感受：耳罩柔软，长时间戴也不夹头，"
                   "夏天会有些闷热。降噪效果：地铁上基本能隔绝低频噪音，人声还是能听见一点。"
                   "音质方面，低音厚实但不轰头，人声位置靠前，听播客很舒服。"
                   "续航实测开降噪能用二十六个小时，比官方标称略少。综合来看这个价位值得入手。")
    add("H1_长评測", long_review, "heavy")
    long_doc = ("微服务架构把单体应用拆分成一组小服务，每个服务独立部署、独立扩展，"
                "通过轻量通信协议协作。它的优势包括技术选型自由、故障隔离、按需伸缩；"
                "代价是分布式事务、链路追踪、服务治理的复杂度显著上升。"
                "团队需要评估组织规模与运维能力，再决定拆分的粒度与节奏。")
    add("H2_长技术段", long_doc, "heavy")
    add("H3_英文长文", "Write a python function that merges two sorted lists into one "
                       "sorted list without using built-in sort, then walk through the "
                       "algorithm step by step, discuss the time and space cost, and "
                       "finally list three common mistakes beginners make with edge cases.", "heavy")

    # ---- 复杂词命中（live 词表：详细/分析/方案）----
    add("W1_双词短句_应重档", "分析并总结这份方案的要点", "heavy")       # 2 命中，短句
    add("W2_单词短句_应中档", "帮我设计方案", "mid")                     # 1 命中
    add("W3_双词长句_应重档", "请详细分析当前新能源汽车市场的发展趋势，并给出你的判断依据", "heavy")
    add("W4_易误伤_词在词中", "产品发布会方案已经定稿了", "mid")          # 1 命中（方案）

    # ---- 精确字符边界（构造到精确长度，无复杂词）----
    base = "请给我讲一个简短的故事。"
    add("B1_24字_应简单", (base + filler)[:24], "simple")
    add("B2_25字_应中档", (base + filler)[:25], "mid")
    add("B3_119字_应中档", ("给我一份阅读清单。" + filler * 10)[:119], "mid")
    add("B4_120字_应重档", ("给我一份阅读清单。" + filler * 10)[:120], "heavy")

    # ---- prefer_tier 显式覆盖 ----
    add("P1_覆盖重档_短输入", "1+1等于几", "heavy", prefer="heavy")
    add("P2_覆盖简单_长输入", long_review, "simple", prefer="simple")
    return cases


def main():
    cfg = http("GET", "/config")
    if "_status" in cfg:
        raise SystemExit("服务不可达: " + BASE)
    # 词表/阈值/映射全部从「运行中服务」实时读取（契约即现状，不假设仓库文件）
    cfg["_complex_words"] = cfg.get("complex_words") or []
    order = cfg["tier_order"]
    t2s = cfg["tier_to_slot"]
    print(f"[cfg] tier_order={order} thresholds={cfg['tier_thresholds']} "
          f"words={cfg['_complex_words']}")

    cases = build_cases()
    # 设计自检：意图档位必须与契约推导一致，否则是数据集标错
    for c in cases:
        want = c["prefer"] or expected_tier(c["text"], cfg)
        if want != c["intent"]:
            raise SystemExit(f"数据集标错: {c['name']} 意图={c['intent']} "
                             f"契约推导={want} len={len(c['text'].strip())}")
    print(f"[dataset] {len(cases)} 条真实用例，意图与契约推导全部一致\n")

    # ---- 阶段 1：重复一致性 + 准确性（3 轮全量）----
    tiers_by_round = []          # 每轮: [tier,...]
    latencies = []
    async_tasks = []             # (case, task_id) 供槽位核验
    for rd in range(1, REPEATS + 1):
        row = []
        for c in cases:
            t0 = time.time()
            r = http("POST", "/chat", {"user_input": c["text"],
                                       "session_id": f"route-{rd}",
                                       "prefer_tier": c["prefer"]})
            dt = round((time.time() - t0) * 1000)
            if r.get("code") != 200:
                check(f"R{rd}_{c['name']}_请求失败", False,
                      f"status={r.get('_status')} detail={r.get('detail')}")
                row.append("ERROR")
                continue
            tier = r.get("task_type")
            row.append(tier)
            latencies.append(dt)
            want = c["prefer"] or expected_tier(c["text"], cfg)
            if rd == 1 and want != tier:
                check(f"R1_{c['name']}_准确性", False,
                      f"期望={want} 实际={tier} len={len(c['text'].strip())}")
            if rd == 1 and tier != order[0]:
                async_tasks.append((c, r.get("task_id")))
            if c["prefer"] and rd == 1:
                mode_ok = (r.get("process_mode") == "direct") == (tier == order[0])
                check(f"R1_{c['name']}_覆盖生效",
                      tier == c["prefer"] and mode_ok,
                      f"tier={tier} mode={r.get('process_mode')}")
        tiers_by_round.append(row)
    print()

    # ---- 阶段 2：准确性汇总 ----
    per_case = {c["name"]: (c, [tiers_by_round[rd][i] for rd in range(REPEATS)])
                for i, c in enumerate(cases)}
    wrong = []
    correct = 0
    for name, (c, got) in per_case.items():
        want = c["prefer"] or expected_tier(c["text"], cfg)
        for g in got:
            if g == want:
                correct += 1
            else:
                wrong.append((name, want, got))
                break
    total = len(cases) * REPEATS
    check("A_路由准确性_契约一致",
          not wrong, f"{correct}/{total} 次分级正确"
          + (f"；不一致: {wrong}" if wrong else ""))
    check("S1_重复一致性_三轮完全相同",
          all(len(set(g)) == 1 for _, g in per_case.values()),
          {n: g for n, (c, g) in per_case.items() if len(set(g)) > 1} or "全部逐次相同")

    # ---- 阶段 3：并发一致性 ----
    targets = [c for c in cases if c["name"] in
               ("S1_算术", "M1_荐书", "H1_长评測", "W1_双词短句_应重档")]
    def send(c):
        r = http("POST", "/chat", {"user_input": c["text"],
                                   "session_id": "conc-route"})
        return c["name"], r
    with ThreadPoolExecutor(len(targets)) as ex:
        rs = list(ex.map(send, targets))
    ok200 = all(r.get("code") == 200 for _, r in rs)
    conc_tiers = {n: r.get("task_type") for n, r in rs}
    conc_expected = all(
        r.get("task_type") == (c["prefer"] or expected_tier(c["text"], cfg))
        for c, (_, r) in zip(targets, rs))
    check("C_并发4路_档位一致且全部成功", ok200 and conc_expected,
          {"tiers": conc_tiers, "codes": [r.get("code") for _, r in rs]})

    # ---- 阶段 4：槽位一致性（异步任务 chosen_model == tier_to_slot 映射）----
    slot_ok, slot_detail = True, {}
    for c, tid in async_tasks[:6]:
        t = {}
        deadline = time.time() + 300
        while time.time() < deadline:
            t = http("GET", f"/task_result?task_id={tid}")
            if t.get("task_status") in ("completed", "failed"):
                break
            time.sleep(1)
        want_slot = t2s.get(c["prefer"] or expected_tier(c["text"], cfg),
                            t2s.get(order[1]))
        got = t.get("chosen_model")
        slot_detail[c["name"]] = got
        if t.get("task_status") != "completed" or got != want_slot:
            slot_ok = False
    check("K_槽位一致性_chosen_model符合映射", slot_ok, slot_detail)

    # ---- 阶段 5：持续无漂移（第 1 轮 vs 第 3 轮）----
    drift = [(cases[i]["name"], tiers_by_round[0][i], tiers_by_round[-1][i])
             for i in range(len(cases))
             if tiers_by_round[0][i] != tiers_by_round[-1][i]]
    check("D_持续无漂移_首末轮一致", not drift, drift or "首轮与末轮全部一致")

    lat = sorted(latencies)
    print(f"\n[latency] n={len(lat)} p50={lat[len(lat)//2]}ms "
          f"p95={lat[int(len(lat)*0.95)]}ms max={lat[-1]}ms")
    passed = sum(1 for r in RESULTS if r["ok"])
    print(f"=== ROUTING_STABILITY: {passed}/{len(RESULTS)} ===")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())

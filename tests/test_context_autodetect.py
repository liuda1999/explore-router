"""上游上下文自动探测功能测试。

- C1（离线）：生效值计算语义——声明值与探测值取 min、未声明用探测值、
  下限 1024、前台窗口联动（FRONT_NUM_CTX / MAX_INPUT_CHARS 重算）；
- C2（离线）：运行期自适应钳制——clamp 后 slot_ctx / 前台预算联动；
- C3（真实）：对运行中的 Ollama 实例直连探测（qwen3.5:4b 应探得
  262144），并验证 record 后生效值参与 slot_ctx；
- C4（真实服务）：BASE 指向运行中的服务时，核验 /config 暴露
  context_window_effective / detected / source。

用法:
  python3 tests/test_context_autodetect.py                      # C1/C2/C3
  BASE=http://127.0.0.1:8100 python3 tests/test_context_autodetect.py  # +C4
"""
import json
import os
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = os.getenv("BASE", "")
MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:200]}")


def offline_tests():
    sys.path.insert(0, str(ROOT))
    import importlib
    from app import config
    importlib.reload(config)

    # 构造一个「已声明 context_window」的前台 api 槽位场景
    key = "front"
    meta = config.MODELS_META[key]
    meta["type"] = "api"                     # 临时视为 api 槽位（仅内存，不落盘）
    meta["context_window"] = 32768
    declared = 32768

    # C1a 探测值 > 声明值：取声明值
    config.record_detected_context(key, 262144, "auto(test)")
    eff = config.SLOT_CTX_EFFECTIVE[key]
    check("C1a_探测大于声明_取min",
          eff["effective"] == declared and eff["declared"] == declared
          and not eff["clamped"] and "auto" in eff["source"],
          eff)
    # C1b 探测值 < 声明值：钳到探测值并标记 clamped
    config.record_detected_context(key, 8192, "auto(test-small)")
    eff = config.SLOT_CTX_EFFECTIVE[key]
    check("C1b_探测小于声明_钳到探测值",
          eff["effective"] == 8192 and eff["clamped"] and eff["detected"] == 8192,
          eff)
    # C1c 前台联动：FRONT_NUM_CTX 与 MAX_INPUT_CHARS 重算
    check("C1c_前台窗口联动",
          config.FRONT_NUM_CTX == 8192 and config.front_ctx_available() > 0
          and config.MAX_INPUT_CHARS >= 512,
          {"front_num_ctx": config.FRONT_NUM_CTX,
           "max_input_chars": config.MAX_INPUT_CHARS})
    # C1d 未声明槽位：清空声明 → 生效 = 探测值
    meta["context_window"] = None
    config.record_detected_context(key, 16384, "auto(test-nodeclared)")
    eff = config.SLOT_CTX_EFFECTIVE[key]
    check("C1d_未声明_生效等于探测值",
          eff["effective"] == 16384 and eff["declared"] is None,
          eff)
    # 恢复声明值（不污染其它测试的内存状态）
    meta["context_window"] = declared
    config.record_detected_context(key, 262144, "auto(test-restore)")

    # C2 运行期自适应钳制
    before = config.slot_ctx(key)
    new = config.clamp_slot_context(key, "unit-test overflow")
    check("C2a_clamp_对半收缩", new == max(1024, before // 2)
          and config.slot_ctx(key) == new,
          {"before": before, "after": new})
    for _ in range(6):   # 连续 clamp 触底
        new = config.clamp_slot_context(key, "unit-test again")
    check("C2b_clamp_下限1024", config.slot_ctx(key) >= 1024
          and config.slot_ctx(key) == 1024, config.slot_ctx(key))
    # 恢复：重新 record 回真实探测值（声明 32768 经 min 语义仍优先——正确行为）
    config.record_detected_context(key, 262144, "auto(ollama /api/show)")
    check("C2c_恢复_min语义生效",
          config.slot_ctx(key) == min(declared, 262144),
          config.slot_ctx(key))
    # local 槽位不受影响
    other = next(k for k, v in config.MODELS_META.items()
                 if v["type"] == "local" and k != key)
    check("C2d_local槽位_不受影响",
          config.SLOT_CTX_EFFECTIVE.get(other) is None
          and config.slot_ctx(other) == config.NUM_CTX,
          {"slot": other, "ctx": config.slot_ctx(other)})


def real_ollama_detect():
    sys.path.insert(0, str(ROOT))
    from app import config
    from app.api_backend import ApiBackend
    # 探测键需要是已注册槽位（record 的守卫）；仓库配置全为 local，注册一个临时 api 槽位
    config.MODELS_META["probe"] = {"type": "api", "provider": "ollama",
                                   "context_window": None, "tier": "probe"}

    async def run():
        b = ApiBackend("probe", OLLAMA_URL, MODEL, provider="ollama")
        await b.start()
        eff = config.SLOT_CTX_EFFECTIVE.get("probe")   # 启动期：架构上限兜底
        await b.chat([{"role": "user", "content": "回复 OK"}], max_tokens=8)
        eff2 = config.SLOT_CTX_EFFECTIVE.get("probe")  # 推理后：运行时校准
        # 推理后模型必然已加载，/api/ps 是权威运行时窗口
        ps = json.loads(urllib.request.urlopen(OLLAMA_URL + "/api/ps",
                                               timeout=5).read())
        runtime = next((int(m["context_length"]) for m in ps.get("models", [])
                        if (m.get("name") or "").startswith(MODEL.split(":")[0])
                        and m.get("context_length")), None)
        await b.stop()
        return eff, eff2, runtime

    import asyncio
    eff, eff2, runtime = asyncio.run(run())
    arch_max = 262144
    check("C3a_启动探测_不高于架构上限",
          eff and eff["effective"] <= arch_max, eff)
    check("C3b_推理后校准_运行时窗口优先",
          eff2 and runtime and eff2["effective"] == runtime
          and "api/ps" in eff2["source"],
          {"runtime(/api/ps)": runtime, "eff2": eff2})


def live_service_check():
    cfg = json.loads(urllib.request.urlopen(BASE + "/config", timeout=5).read())
    slots = cfg.get("slots") or {}
    api_slots = {k: s for k, s in slots.items() if s.get("type") == "api"}
    if not api_slots:
        check("C4_服务槽位", False, "BASE 服务没有 api 槽位")
        return
    with_src = [s.get("context_window_source") for s in api_slots.values()]
    check("C4_config暴露_探测来源与生效值",
          all(s.get("context_window_effective") for s in api_slots.values())
          and any(src and "auto" in src for src in with_src),
          {k: {"eff": s.get("context_window_effective"),
               "src": s.get("context_window_source")}
           for k, s in api_slots.items()})


def main():
    offline_tests()
    real_ollama_detect()
    if BASE:
        live_service_check()
    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"=== CONTEXT_AUTODETECT: {passed}/{len(RESULTS)} ===")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    import urllib.error  # noqa
    raise SystemExit(main())

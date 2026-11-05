"""CI 静态层：配置解析与启动期校验。

不依赖 GPU / 模型文件 / FastAPI，可在普通 CI runner 上运行。
错误配置用真实 YAML 文件 + 子进程验证 fail-fast，不是 mock。
"""
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="explore-ci-")
os.environ.setdefault("DATA_DIR", os.path.join(TMP, "data"))
os.environ.setdefault("LOG_DIR", os.path.join(TMP, "logs"))
sys.path.insert(0, str(ROOT))

CASES = []


def check(name, ok, detail):
    CASES.append({"case": name, "ok": bool(ok), "detail": str(detail)[:600]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def load_base():
    with open(ROOT / "settings.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_import(settings_path, env_extra=None):
    env = os.environ.copy()
    env["SETTINGS_FILE"] = str(settings_path)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "pylib")])
    env["DATA_DIR"] = os.path.join(TMP, "data")
    env["LOG_DIR"] = os.path.join(TMP, "logs")
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.run(
        [sys.executable, "-c",
         "from app import config; print('TIER_ORDER=', config.TIER_ORDER, "
         "'FRONT=', config.FRONT_KEY, 'CTX=', config.NUM_CTX, "
         "'BACKEND=', config.GPU_BACKEND, "
         "'FRONT_DEVICES=', config.MODELS_META[config.FRONT_KEY]['devices'])"],
        env=env, capture_output=True, text=True, timeout=120)


def write_variant(name, mutate):
    data = load_base()
    mutate(data)
    path = pathlib.Path(TMP) / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


def main():
    from app import config

    check("resident_single",
          sum(1 for v in config.MODELS_META.values() if v["resident"]) == 1,
          f"front={config.FRONT_KEY}")
    check("tier_order_three", len(config.TIER_ORDER) == 3, config.TIER_ORDER)
    check("tier_mapping_complete",
          all(t in config.TIER_TO_SLOT for t in config.TIER_ORDER[:3])
          and all(s in config.MODELS_META for s in config.TIER_TO_SLOT.values()),
          config.TIER_TO_SLOT)
    check("num_ctx_positive", config.NUM_CTX > 0,
          f"{config.NUM_CTX} ({config.NUM_CTX_SOURCE})")
    check("input_limit_positive", config.MAX_INPUT_CHARS > 0, config.MAX_INPUT_CHARS)
    check("queue_limit_non_negative", config.QUEUE_MAXSIZE >= 0, config.QUEUE_MAXSIZE)
    check("history_budget_valid",
          0 < config.HISTORY_BUDGET_RATIO < 0.9
          and config.HISTORY_MIN_CHARS >= 128
          and config.HISTORY_MAX_MESSAGES >= 2,
          f"ratio={config.HISTORY_BUDGET_RATIO} "
          f"min_chars={config.HISTORY_MIN_CHARS} "
          f"max_messages={config.HISTORY_MAX_MESSAGES}")
    check("context_budget_fits",
          config.MAX_INPUT_CHARS + config.HISTORY_MIN_CHARS
          + config.FRONT_MAX_TOKENS + config.CONTEXT_SAFETY_TOKENS
          + len(config.FRONT_SYSTEM) <= config.NUM_CTX,
          f"input={config.MAX_INPUT_CHARS} + history_min={config.HISTORY_MIN_CHARS} "
          f"+ output={config.FRONT_MAX_TOKENS} + safety={config.CONTEXT_SAFETY_TOKENS} "
          f"+ system={len(config.FRONT_SYSTEM)} <= ctx={config.NUM_CTX}")
    check("vram_plan_complete",
          all(k in config.VRAM_PLAN for k in config.MODELS_META)
          and all({"vram_mb", "available", "reason"} <= set(v)
                  for v in config.VRAM_PLAN.values()),
          {k: (v["vram_mb"], v["available"]) for k, v in config.VRAM_PLAN.items()})
    check("backend_known", config.GPU_BACKEND in ("cuda", "rocm", "cpu"),
          f"{config.GPU_BACKEND} gpus={config.DEVICE_COUNT}")

    # ---- CPU 后端路径：不启动模型，只验证配置解析不依赖 GPU ----
    r = run_import(ROOT / "settings.yaml", {"GPU_BACKEND": "cpu"})
    check("cpu_backend_parses",
          r.returncode == 0 and "BACKEND= cpu" in (r.stdout or "")
          and "FRONT_DEVICES= []" in (r.stdout or ""),
          (r.stdout or r.stderr or "").strip()[:300])

    # ---- 显式设备越界：有 GPU 时必须 fail-fast；无 GPU 环境跳过 ----
    if config.DEVICE_COUNT > 0:
        def bad_devices(d):
            d["slots"]["mid"]["placement"] = "explicit"
            d["slots"]["mid"]["devices"] = [config.DEVICE_COUNT + 5]

        p = write_variant("bad_devices", bad_devices)
        r = run_import(p)
        check("reject_device_out_of_range",
              r.returncode != 0 and "超出实际卡数" in (r.stderr or ""),
              (r.stderr or "").strip().splitlines()[-1][:300] if r.stderr else "no error")
    else:
        print("[SKIP] reject_device_out_of_range: 当前无 GPU（DEVICE_COUNT=0）")

    # ---- 错误配置必须启动失败，并给出可读原因 ----
    def no_resident(d):
        for v in d["slots"].values():
            v["resident"] = False

    p = write_variant("no_resident", no_resident)
    r = run_import(p)
    check("reject_no_resident", r.returncode != 0 and "resident" in (r.stderr or ""),
          (r.stderr or "").strip().splitlines()[-1][:300] if r.stderr else "no error")

    def bad_slot(d):
        d["routing"]["tier_to_slot"]["mid"] = "slot-not-exist"

    p = write_variant("bad_slot", bad_slot)
    r = run_import(p)
    check("reject_bad_slot",
          r.returncode != 0 and "不存在的槽位" in (r.stderr or ""),
          (r.stderr or "").strip().splitlines()[-1][:300] if r.stderr else "no error")

    def bad_threshold(d):
        d["routing"]["mid_min_chars"] = 500
        d["routing"]["heavy_min_chars"] = 100

    p = write_variant("bad_threshold", bad_threshold)
    r = run_import(p)
    check("reject_bad_threshold",
          r.returncode != 0 and "mid_min_chars" in (r.stderr or ""),
          (r.stderr or "").strip().splitlines()[-1][:300] if r.stderr else "no error")

    # ---- 档位改名应该可配置，而不是被代码写死 ----
    def renamed_tiers(d):
        d["routing"]["tier_order"] = ["quick", "standard", "deep"]
        d["routing"]["tier_to_slot"] = {
            "quick": "front", "standard": "mid", "deep": "moe"}

    p = write_variant("renamed_tiers", renamed_tiers)
    r = run_import(p)
    check("custom_tier_names_accepted",
          r.returncode == 0 and "['quick', 'standard', 'deep']" in (r.stdout or ""),
          (r.stdout or r.stderr or "").strip()[:300])

    passed = sum(1 for x in CASES if x["ok"])
    print(f"\n=== CONFIG {passed}/{len(CASES)} ===")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())

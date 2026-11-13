"""CLI 离线功能测试：config validate/set/reset/get 与 status（不依赖运行中的服务）。

用临时 fixture settings.yaml 跑真实子进程 `python3 cli.py ...`，
验证覆盖层写入、reset 恢复 settings.yaml 基础值、非法参数报错。
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append({"case": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:200]}")


def run_cli(args, env):
    return subprocess.run(
        [sys.executable, str(ROOT / "cli.py")] + args,
        env=env, capture_output=True, text=True, timeout=120, cwd=str(ROOT))


def fixture(tmp):
    import yaml
    cfg = {
        "runtime": {"server_bin": "llama-server", "lib_dir": "",
                    "gpu_backend": "auto", "n_gpu_layers": 99,
                    "max_output_tokens": 1024, "queue_maxsize": 32,
                    "ports": {"api": 18941, "front": 18951, "slot": 18961},
                    "startup_timeout_s": 120, "request_timeout_s": 600},
        "context": {"num_ctx": 32768, "kv_cache": "q8_0"},
        "embedding": {"onnx": "models/embedding/model.onnx",
                      "tokenizer": "models/embedding/tokenizer.json"},
        "budget": {"reserve_system_mb": 1024, "headroom_ratio": 0.10},
        "slots": {
            "front": {"type": "api", "base_url": "http://127.0.0.1:18940/v1",
                      "model": "fake-front", "context_window": 32768,
                      "role": "front", "resident": True},
            "mid": {"type": "api", "base_url": "http://127.0.0.1:18940/v1",
                    "model": "fake-cloud", "context_window": 32768,
                    "role": "middle"},
        },
        "routing": {
            "tier_order": ["simple", "mid", "heavy"],
            "tier_to_slot": {"simple": "front", "mid": "mid", "heavy": "mid"},
            "default_slot": "mid",
            "mid_min_chars": 25, "heavy_min_chars": 120,
            "max_input_chars": "auto",
            "complex_words": ["分析"],
        },
        "openai": {"default_mode": "final", "wait_timeout_s": 60},
        "frontend": {"max_tokens": 256, "history_budget_ratio": 0.45,
                     "history_min_chars": 2048, "history_max_messages": 40,
                     "context_safety_tokens": 256, "restart_cooldown_s": 30,
                     "progress_window_s": 1800,
                     "system_prompt": "你是测试前台。"},
    }
    path = pathlib.Path(tmp) / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


def main():
    import shutil
    tmp = tempfile.mkdtemp(prefix="explore-cli-")
    try:
        return _run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp):
    settings = fixture(tmp)
    overlay = pathlib.Path(tmp) / "settings.local.yaml"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "pylib")]),
        "SETTINGS_FILE": str(settings),
        "SETTINGS_LOCAL_FILE": str(overlay),
        "DATA_DIR": str(pathlib.Path(tmp) / "data"),
        "LOG_DIR": str(pathlib.Path(tmp) / "logs"),
    })

    r = run_cli(["config", "validate"], env)
    check("config_validate", r.returncode == 0 and "CONFIG_OK" in r.stdout,
          (r.stdout or r.stderr).strip()[:160])

    r = run_cli(["config", "get", "front_slot"], env)
    check("config_get_front", r.returncode == 0 and "front" in r.stdout,
          r.stdout.strip()[:120])

    r = run_cli(["config", "get", "limits.front_max_tokens"], env)
    check("config_get_dotted", r.returncode == 0 and "256" in r.stdout,
          r.stdout.strip()[:120])

    r = run_cli(["config", "set", "FRONT_MAX_TOKENS", "311"], env)
    check("config_set", r.returncode == 0 and "311" in r.stdout
          and overlay.exists()
          and "311" in overlay.read_text(encoding="utf-8"),
          (r.stdout or r.stderr).strip()[:160])

    r = run_cli(["config", "get", "limits.front_max_tokens"], env)
    check("overlay_visible_in_new_process", r.returncode == 0 and "311" in r.stdout,
          r.stdout.strip()[:120])

    r = run_cli(["config", "reset", "FRONT_MAX_TOKENS"], env)
    check("config_reset_restores_yaml_base",
          r.returncode == 0 and "256" in r.stdout
          and "max_tokens" not in overlay.read_text(encoding="utf-8"),
          (r.stdout or r.stderr).strip()[:160])

    r = run_cli(["config", "set", "NOT_EXIST", "1"], env)
    check("reject_unknown_param",
          r.returncode != 0 and "不可热调" in (r.stdout + r.stderr),
          (r.stdout + r.stderr).strip()[:160])

    r = run_cli(["status"], env)
    check("status_not_running", r.returncode == 0 and "not running" in r.stdout,
          r.stdout.strip()[:120])

    r = run_cli(["logs", "--tail", "3"], env)
    check("logs_missing_friendly", "日志不存在" in (r.stdout + r.stderr),
          (r.stdout + r.stderr).strip()[:120])

    passed = sum(1 for x in RESULTS if x["ok"])
    print(f"\n=== CLI {passed}/{len(RESULTS)} ===")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

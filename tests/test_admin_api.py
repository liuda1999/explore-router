"""管理接口热调测试：POST /admin/config 立即生效 + 覆盖层持久化 + reset 恢复。

需要运行中的服务（BASE 环境变量，默认 http://127.0.0.1:8100）。
临时修改 FRONT_MAX_TOKENS / COMPLEX_WORDS，结束全部恢复，不遗留覆盖层。

⚠️ 本套件会改动「被测服务」的运行参数并触发两次真实重启：请指向隔离实例
（独立 DATA_DIR / SETTINGS_FILE / API_PORT 的 fixture 服务），不要对着生产
实例跑——若中途失败，检查服务的 settings.local.yaml 是否有遗留覆盖项。
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = os.getenv("BASE", "http://127.0.0.1:8100")
TOKEN = os.getenv("ADMIN_TOKEN", "")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append({"case": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:200]}")


def http(method, path, body=None, timeout=30, headers_extra=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["X-Admin-Token"] = TOKEN
    headers.update(headers_extra or {})
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw}


def hot_param(admin, name, field="current"):
    return (admin.get("hot_params") or {}).get(name, {}).get(field)


def main():
    # 1) 结构
    status, admin = http("GET", "/admin/config")
    check("admin_config_shape",
          status == 200 and isinstance(admin.get("hot_params"), dict)
          and "FRONT_MAX_TOKENS" in admin["hot_params"],
          f"{len(admin.get('hot_params') or {})} hot params")

    # 2) 非法请求必须被拒绝
    status, r = http("POST", "/admin/config", {"NOT_A_PARAM": 1})
    check("reject_unknown_param", status == 400 and "不可热调" in str(r.get("detail", "")),
          r.get("detail"))
    status, r = http("POST", "/admin/config", {"FRONT_MAX_TOKENS": 1})
    check("reject_out_of_range", status == 400, r.get("detail"))
    status, r = http("POST", "/admin/config", {"MID_MIN_CHARS": 999999})
    check("reject_threshold_conflict", status == 400
          and ("递增" in str(r.get("detail")) or "冲突" in str(r.get("detail"))),
          r.get("detail"))

    # 3) 热调 FRONT_MAX_TOKENS：立即生效 + 落盘 + 行为反映
    base_val = hot_param(admin, "FRONT_MAX_TOKENS", "base")
    overlay_file = admin.get("overlay_file")
    status, r = http("POST", "/admin/config", {"FRONT_MAX_TOKENS": 300})
    check("set_front_max_tokens", status == 200
          and r.get("applied", {}).get("FRONT_MAX_TOKENS") == 300, r.get("applied"))
    status, admin2 = http("GET", "/admin/config")
    check("current_reflects_change",
          hot_param(admin2, "FRONT_MAX_TOKENS") == 300
          and hot_param(admin2, "FRONT_MAX_TOKENS", "changed") is True,
          hot_param(admin2, "FRONT_MAX_TOKENS"))
    check("overlay_persisted",
          overlay_file and pathlib.Path(overlay_file).exists()
          and "300" in pathlib.Path(overlay_file).read_text(encoding="utf-8"),
          overlay_file)
    # 覆盖层重新导入（模拟重启后的持久化语义）
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "pylib")])
    env["SETTINGS_FILE"] = admin.get("settings_file") or ""
    env["SETTINGS_LOCAL_FILE"] = overlay_file or ""
    code = ("from app import config; print('VALUE=', config.FRONT_MAX_TOKENS)")
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=60)
    check("overlay_reimport_applies", "VALUE= 300" in (r.stdout or ""),
          (r.stdout or r.stderr or "").strip()[:200])

    # 4) 行为反映：/chat 的 context_usage.front_max_tokens 变为 300
    _s, chat = http("POST", "/chat",
                    {"user_input": "hi", "session_id": "admin-test"})
    check("behavior_reflects_hot_param",
          (chat.get("context_usage") or {}).get("front_max_tokens") == 300,
          (chat.get("context_usage") or {}).get("front_max_tokens"))

    # 5) 词表热调影响分级
    tier_mid = (admin2.get("tier_order") or ["simple", "mid"])[1]
    status, _r = http("POST", "/admin/config", {"COMPLEX_WORDS": ["ADMINW1"]})
    _s, chat = http("POST", "/chat",
                    {"user_input": "ADMINW1", "session_id": "admin-test"})
    check("complex_words_hot_applied", chat.get("task_type") == tier_mid,
          {"tier": chat.get("task_type"), "expect": tier_mid})

    # 6) reset 恢复基础值并清理覆盖层
    status, r = http("DELETE", "/admin/config/FRONT_MAX_TOKENS")
    check("reset_front_max_tokens", status == 200 and r.get("value") == base_val,
          {"value": r.get("value"), "base": base_val})
    status, r = http("DELETE", "/admin/config/COMPLEX_WORDS")
    check("reset_complex_words", status == 200, r.get("value"))
    _s, chat = http("POST", "/chat",
                    {"user_input": "ADMINW1", "session_id": "admin-test"})
    check("behavior_restored_after_reset", chat.get("task_type") != tier_mid
          or "ADMINW1" in json.dumps(chat.get("selection_reason") or ""),
          {"tier": chat.get("task_type")})
    _s, admin3 = http("GET", "/admin/config")
    check("no_leftover_overrides",
          all(not hot_param(admin3, k, "changed")
              for k in ("FRONT_MAX_TOKENS", "COMPLEX_WORDS")),
          {k: hot_param(admin3, k, "changed")
           for k in ("FRONT_MAX_TOKENS", "COMPLEX_WORDS")})

    # ---- 配置管理（启动期参数，覆盖层 + 预检 + 重启生效）----
    status, schema = http("GET", "/admin/settings")
    paths = {f["path"] for f in schema.get("fields") or []}
    check("settings_schema_shape",
          status == 200 and "slots.front.type" in paths
          and "runtime.ports.api" in paths
          and all(f.get("desc") for f in (schema.get("fields") or [])[:5]),
          f"{len(paths)} fields")

    def wait_ready(prev_started_at=None, timeout=150):
        """轮询服务恢复：以 /health 可用且 started_at 已变化为准（确认是新实例）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _s, h = http("GET", "/health", timeout=5)
                if h.get("code") == 200 and (
                        prev_started_at is None
                        or h.get("started_at") not in (None, prev_started_at)):
                    return True
            except SystemExit:
                pass
            time.sleep(1.5)
        return False

    # 7) 合法更新：tier_fallbacks 写覆盖层，预检通过
    status, r = http("POST", "/admin/settings", {"updates": {
        "routing.tier_fallbacks": {"heavy": ["cloud"]}}})
    check("settings_update_saved", status == 200
          and "重启" in str(r.get("note", "")), r.get("note"))
    # 8) 非法更新：前台取消 resident（预检必须拒绝）
    status, r = http("POST", "/admin/settings", {"updates": {
        "slots.front.resident": False}})
    check("settings_update_rejected_by_precheck",
          status == 400 and "预检失败" in str(r.get("detail", "")),
          r.get("detail"))
    # 9) resets：无覆盖值的字段报错
    status, r = http("POST", "/admin/settings", {"resets": ["runtime.gpu_backend"]})
    check("settings_reset_without_overlay_rejected",
          status == 400 and "没有覆盖层值" in str(r.get("detail", "")),
          r.get("detail"))

    # 10) 重启：覆盖层改动生效
    _s, h0 = http("GET", "/health")
    prev_started = h0.get("started_at")
    status, r = http("POST", "/admin/restart")
    check("restart_accepted", status == 200 and r.get("status") == "restarting",
          r.get("note"))
    check("restart_recovers", wait_ready(prev_started), "轮询新实例恢复可用")
    _s, cfg2 = http("GET", "/config")
    fb = (cfg2.get("tier_fallbacks") or {})
    check("restart_applies_overlay", fb.get("heavy") == ["cloud"], fb)

    # 11) 清理：移除测试覆盖并再次重启
    status, r = http("POST", "/admin/settings", {"resets": ["routing.tier_fallbacks"]})
    check("settings_cleanup", status == 200, r.get("note"))
    _s, h1 = http("GET", "/health")
    _s, _r = http("POST", "/admin/restart")
    check("restart_after_cleanup", wait_ready(h1.get("started_at")), "二次重启恢复")
    _s, cfg3 = http("GET", "/config")
    check("cleanup_effective", "heavy" not in (cfg3.get("tier_fallbacks") or {}),
          cfg3.get("tier_fallbacks"))

    # ---- 安全修复回归：api_key 掩码、CSRF 守卫、CLI 热路径 ----
    # 12) 明文密钥只出掩码；掩码值写入被丢弃（不落覆盖层）
    status, r = http("POST", "/admin/settings",
                     {"updates": {"slots.cloud.api_key": "sk-test-123"}})
    check("api_key_set_ok", status == 200, r.get("note"))
    status, schema2 = http("GET", "/admin/settings")
    ak = {f["path"]: f for f in schema2["fields"]}["slots.cloud.api_key"]
    # 安全不变量：明文密钥绝不进入响应（value=当前生效值，重启前仍为空；
    # overlay_value=待生效值，两者都必须是掩码或空）
    check("api_key_masked_in_schema",
          ak["overlay_value"] == "***" and ak["value"] in ("", "***")
          and "sk-" not in json.dumps([ak["value"], ak["overlay_value"]]),
          {"value": ak["value"], "overlay": ak["overlay_value"]})
    status, r = http("POST", "/admin/settings",
                     {"updates": {"slots.cloud.api_key": "***"}})
    check("api_key_mask_value_dropped",
          status == 400 and "没有任何变更" in str(r.get("detail", "")),
          r.get("detail"))
    status, r = http("POST", "/admin/settings",
                     {"resets": ["slots.cloud.api_key"]})
    check("api_key_cleanup", status == 200, r.get("note"))

    # 13) CSRF 守卫：非 JSON Content-Type 与跨站 Origin 必须被拒绝
    status, r = http("POST", "/admin/config",
                     {"FRONT_MAX_TOKENS": 256},
                     headers_extra={"Content-Type": "text/plain"})
    check("csrf_content_type_rejected",
          status == 415 and "Content-Type" in str(r.get("detail", "")),
          {"status": status, "detail": r.get("detail")})
    status, r = http("POST", "/admin/config",
                     {"FRONT_MAX_TOKENS": 256},
                     headers_extra={"Origin": "http://evil.example.com"})
    check("csrf_cross_origin_rejected",
          status == 403 and "Origin" in str(r.get("detail", "")),
          {"status": status, "detail": r.get("detail")})
    # 同源 Origin 应当放行（浏览器控制台场景）
    origin = "/".join(BASE.split("/")[:3])
    status, r = http("POST", "/admin/config",
                     {"FRONT_MAX_TOKENS": 256},
                     headers_extra={"Origin": origin})
    check("csrf_same_origin_allowed",
          status == 200 and r["applied"]["FRONT_MAX_TOKENS"] == 256,
          {"status": status})
    _s, _r = http("DELETE", "/admin/config/FRONT_MAX_TOKENS")

    # 14) CLI config set 对运行中的服务走热生效路径（修复前只写覆盖层不生效）
    _s, adm = http("GET", "/admin/config")
    cli_env = os.environ.copy()
    cli_env.update({
        "PYTHONPATH": f"{ROOT}{os.pathsep}{ROOT / 'pylib'}",
        "API_PORT": BASE.rsplit(":", 1)[1],
        "SETTINGS_FILE": adm.get("settings_file") or "",
        "SETTINGS_LOCAL_FILE": adm.get("overlay_file") or "",
        "DATA_DIR": (adm.get("paths") or {}).get("data") or "",
        "LOG_DIR": (adm.get("paths") or {}).get("logs") or "",
    })
    r = subprocess.run(
        [sys.executable, str(ROOT / "cli.py"), "config", "set",
         "COMPACTION_MIN_DROPPED_CHARS", "700"],
        env=cli_env, capture_output=True, text=True, timeout=60,
        cwd=str(ROOT))
    check("cli_config_set_hot_applies",
          r.returncode == 0 and "热生效" in (r.stdout or ""),
          (r.stdout or r.stderr or "").strip()[:160])
    _s, adm2 = http("GET", "/admin/config")
    check("cli_set_visible_in_service",
          hot_param(adm2, "COMPACTION_MIN_DROPPED_CHARS") == 700,
          hot_param(adm2, "COMPACTION_MIN_DROPPED_CHARS"))
    r = subprocess.run(
        [sys.executable, str(ROOT / "cli.py"), "config", "reset",
         "COMPACTION_MIN_DROPPED_CHARS"],
        env=cli_env, capture_output=True, text=True, timeout=60,
        cwd=str(ROOT))
    check("cli_config_reset_hot_applies",
          r.returncode == 0 and "热生效" in (r.stdout or ""),
          (r.stdout or r.stderr or "").strip()[:160])

    passed = sum(1 for x in RESULTS if x["ok"])
    print(f"\n=== ADMIN_API {passed}/{len(RESULTS)} ===")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

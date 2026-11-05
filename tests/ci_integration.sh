#!/bin/bash
# CI 集成层：必须在带 GPU、已部署模型与 pylib 的 self-hosted runner 上运行。
# 会用独立 DATA_DIR/PID/日志目录，避免污染生产状态；结束自动停服。
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
export PYTHONPATH="$R/pylib:$R"
export DATA_DIR="${DATA_DIR:-$R/data/ci}"
export LOG_DIR="${LOG_DIR:-$R/logs/ci}"
export PID_FILE="${PID_FILE:-$R/data/ci-router.pid}"
export LOG="${LOG:-$R/logs/ci-api.log}"
export API_PORT="${API_PORT:-18110}"
export FRONT_PORT="${FRONT_PORT:-18120}"
export SLOT_PORT="${SLOT_PORT:-18121}"
export BASE="http://127.0.0.1:$API_PORT"
mkdir -p "$DATA_DIR" "$LOG_DIR"

fail=0
cleanup() { ./run.sh stop >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "### 检查运行时与模型"
if ! python3 - <<'PY'
import pathlib
import sys
from app import config

problems = []
if config.GPU_BACKEND not in ("cuda", "rocm"):
    problems.append(f"GPU 后端不可用: {config.GPU_BACKEND}")
for key, meta in config.MODELS_META.items():
    if not pathlib.Path(meta["path"]).exists():
        problems.append(f"缺少模型 {key}: {meta['path']}")
if not pathlib.Path(config.LLAMA_SERVER).exists() and "/" in config.LLAMA_SERVER:
    problems.append(f"运行时不存在: {config.LLAMA_SERVER}")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print(f"backend={config.GPU_BACKEND} gpus={config.DEVICE_COUNT} "
      f"ctx={config.NUM_CTX} slots={list(config.MODELS_META)}")
PY
then
  echo "INTEGRATION_PRECONDITION_FAILED"
  exit 1
fi

echo "### 启动服务（隔离数据目录）"
./run.sh stop >/dev/null 2>&1 || true
if ! ./run.sh start --wait; then
  echo "START_FAILED"; tail -40 "$LOG"; exit 1
fi

run() {
  local name="$1"; shift
  echo; echo "### $name"
  "$@" || fail=1
}

run "基础冒烟" python3 tests/test_smoke.py
CTX="$(curl -s --max-time 10 "$BASE/config" | python3 -c 'import json,sys; print(json.load(sys.stdin)["num_ctx"])')"
run "单档完整矩阵 ctx=$CTX" python3 tests/test_matrix.py --ctx "$CTX"
run "审计：API/内容/并发" python3 tests/audit_api.py
run "审计：长上下文/长文本" python3 tests/audit_long_context.py
run "审计：故障注入" python3 tests/audit_faults.py
run "审计：重启恢复/队列上限" python3 tests/audit_restart.py
run "连续请求稳定性" python3 tests/test_stability.py

echo
if [ "$fail" -eq 0 ]; then echo "INTEGRATION_OK"; else echo "INTEGRATION_FAILED"; fi
exit "$fail"

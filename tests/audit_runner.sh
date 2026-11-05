#!/bin/bash
# 手动审计入口：在已启动的服务上跑 API / 故障 / 重启三类真实审计。
# 用法: BASE=http://127.0.0.1:8100 ./tests/audit_runner.sh
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
export PYTHONPATH="$R/pylib:$R"
export BASE="${BASE:-http://127.0.0.1:8100}"

fail=0
echo "### 审计目标 $BASE"
curl -sf --max-time 10 "$BASE/health" >/dev/null || {
  echo "服务不可达；请先 ./run.sh start --wait"; exit 1; }

for t in audit_api audit_faults audit_restart; do
  echo; echo "########## $t ##########"
  python3 "tests/$t.py" || fail=1
done

echo
for f in logs/audit_api.json logs/audit_faults.json logs/audit_restart.json; do
  [ -f "$f" ] || continue
  python3 - "$f" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(sys.argv[1], "all_pass=", d.get("all_pass"),
      "passed=", d.get("passed", d.get("hard_passed")),
      "total=", d.get("total", d.get("hard_total")))
PY
done

if [ "$fail" -eq 0 ]; then echo "AUDIT_OK"; else echo "AUDIT_FAILED"; fi
exit "$fail"

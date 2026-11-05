#!/bin/bash
# 按 32K -> 64K -> 96K 三档，每档重启服务并跑完整功能 + 速度测试。
# 每档独立：启动失败也会记录并继续下一档，不中断矩阵。
# 路径取自脚本位置，端口取自 settings.yaml，不写死设备或目录。
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
export PYTHONPATH="$R/pylib:$R"
API_PORT="${API_PORT:-8100}"

# 测试档位可按设备能力覆盖：CTX_MATRIX="8192 16384 32768" ./tests/run_matrix.sh
CTX_MATRIX="${CTX_MATRIX:-32768 65536 98304}"
for CTX in $CTX_MATRIX; do
  echo "########## CTX=$CTX $(date +%T) ##########"
  NUM_CTX=$CTX ./run.sh stop >/dev/null 2>&1
  if ! NUM_CTX=$CTX ./run.sh start --wait; then
    echo "START_FAILED CTX=$CTX"
    tail -12 "$R/logs/api.log"
    tail -8 "$R/logs/front.log" 2>/dev/null
    python3 -c "import json,pathlib; pathlib.Path('$R/logs/matrix_$CTX.json').write_text(json.dumps({'ctx':$CTX,'start_failed':True,'all_pass':False},ensure_ascii=False,indent=2))"
    continue
  fi
  python3 tests/test_matrix.py --ctx "$CTX" || true
  echo "--- CTX=$CTX done $(date +%T) ---"
done

echo "=== matrix complete $(date +%T) ==="
ls -lh "$R"/logs/matrix_*.json 2>/dev/null || true
for f in "$R"/logs/matrix_*.json; do
  [ -e "$f" ] || continue
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(sys.argv[1].split('/')[-1], 'all_pass=',d.get('all_pass'),'start_failed=',d.get('start_failed'),'cases=',len(d.get('cases',[])),'speeds=',[(s.get('label'),s.get('predicted_per_second')) for s in d.get('speeds',[])])" "$f"
done

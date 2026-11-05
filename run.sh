#!/bin/bash
# 幂等启停：只管理本工作区的进程，不做全局清理。
# 用法: ./run.sh start [--wait] | stop | restart | status | health
# 路径与端口都不写死：目录取自脚本位置，端口取自 settings.yaml（可被环境变量覆盖）。
set -u

R="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$R" || exit 1
export PYTHONPATH="$R/pylib:$R"

PID_FILE="${PID_FILE:-$R/data/router.pid}"
LOG="${LOG:-$R/logs/api.log}"

_cfg_port() {
  python3 -c "from app import config; print($1)" 2>/dev/null
}

API_PORT="${API_PORT:-$(_cfg_port 'config.API_PORT')}"
API_PORT="${API_PORT:-8100}"
FRONT_PORT="${FRONT_PORT:-$(_cfg_port 'config.FRONT_PORT')}"
FRONT_PORT="${FRONT_PORT:-18100}"
SLOT_PORT="${SLOT_PORT:-$(_cfg_port 'config.SLOT_PORT')}"
SLOT_PORT="${SLOT_PORT:-18101}"

port_busy() {
  (ss -tln 2>/dev/null || netstat -tln 2>/dev/null) | grep -q ":$1 "
}

port_pid() {
  (ss -tlnp 2>/dev/null || true) | grep ":$1 " | grep -o 'pid=[0-9]*' \
    | head -1 | cut -d= -f2
}

wait_ready() {
  local tries="${1:-120}" i
  for i in $(seq 1 "$tries"); do
    if curl -sf --max-time 3 "http://127.0.0.1:$API_PORT/health" 2>/dev/null \
        | grep -q '"frontend":"ready"'; then
      return 0
    fi
    sleep 2
  done
  return 1
}

case "$1" in
  start)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "already running pid=$(cat "$PID_FILE")"
      exit 0
    fi
    if port_busy "$API_PORT"; then
      echo "ERROR: port $API_PORT already in use; 请改 settings.yaml 的 runtime.ports.api 或先释放端口" >&2
      exit 1
    fi
    # 启动前预检前台显存：不足时直接报清楚，避免 --wait 白等
    FRONT_CHECK="$(python3 -c "from app import config; p=config.VRAM_PLAN.get(config.FRONT_KEY,{}); print('OK' if p.get('available') else 'NO:'+str(p.get('reason')))" 2>/dev/null)"
    case "$FRONT_CHECK" in
      OK) ;;
      NO:*) echo "ERROR: 前台槽位显存不足 ${FRONT_CHECK#NO:}" >&2
            echo "       请释放显存，或调整 settings.yaml 的模型/量化/vram_mb" >&2
            exit 1 ;;
      *) echo "WARN: 无法预检显存（配置可能未就绪），继续尝试启动" >&2 ;;
    esac
    nohup python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" --workers 1 \
      >> "$LOG" 2>&1 &
    echo $! > "$PID_FILE"
    echo "started pid=$(cat "$PID_FILE") api=$API_PORT log=$LOG"
    if [ "${2:-}" = "--wait" ]; then
      if wait_ready 210; then echo "ready"; else
        echo "NOT READY; see $LOG" >&2; tail -20 "$LOG" >&2; exit 1
      fi
    fi
    ;;
  stop)
    if [ -f "$PID_FILE" ]; then
      P="$(cat "$PID_FILE")"
      kill "$P" 2>/dev/null
      for _ in $(seq 1 20); do
        kill -0 "$P" 2>/dev/null || break
        sleep 1
      done
      kill -9 "$P" 2>/dev/null
      rm -f "$PID_FILE"
    fi
    # 兜底清理本项目模型端口：只杀命令行包含本项目目录的进程，避免误伤其它进程
    for p in "$FRONT_PORT" "$SLOT_PORT"; do
      id="$(port_pid "$p")"
      if [ -n "$id" ]; then
        cmd="$(tr '\0' ' ' < "/proc/$id/cmdline" 2>/dev/null)"
        case "$cmd" in
          *"$R"*) kill "$id" 2>/dev/null ;;
          *) echo "skip pid=$id on port $p (not ours): ${cmd:-unknown}" >&2 ;;
        esac
      fi
    done
    echo "stopped"
    ;;
  restart)
    "$0" stop
    shift
    exec "$0" start "$@"
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "running pid=$(cat "$PID_FILE") api=$API_PORT front=$FRONT_PORT slot=$SLOT_PORT"
    else
      echo "not running (api=$API_PORT front=$FRONT_PORT slot=$SLOT_PORT)"
    fi
    ;;
  health)
    curl -s --max-time 10 "http://127.0.0.1:$API_PORT/health"
    echo
    ;;
  *)
    echo "usage: $0 start [--wait] | stop | restart | status | health"; exit 1
    ;;
esac

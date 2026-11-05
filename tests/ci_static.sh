#!/bin/bash
# CI 静态层：无需 GPU / 模型 / FastAPI，可在任意 Linux runner 运行。
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
export PYTHONPATH="$R/pylib:$R"

fail=0
step() { echo; echo "### $*"; }

step "Python 语法编译"
python3 -m compileall -q app tests || fail=1

step "Shell 语法"
bash -n run.sh || fail=1
for f in tests/*.sh; do
  [ -e "$f" ] || continue
  bash -n "$f" || fail=1
done

step "配置解析与启动期校验"
python3 tests/check_config.py || fail=1

step "可移植性（无设备/模型硬编码）"
python3 tests/check_portability.py || fail=1

step "文档一致性"
python3 tests/check_docs.py || fail=1

step "静态检查（ruff: 语法/未定义名）"
if command -v ruff >/dev/null 2>&1; then
  ruff check --select E9,F63,F7,F82 app tests || fail=1
else
  echo "ruff 未安装，跳过（CI 中会安装）"
fi

echo
if [ "$fail" -eq 0 ]; then echo "STATIC_OK"; else echo "STATIC_FAILED"; fi
exit "$fail"

#!/bin/bash
# launchd 调用的包装脚本。存在的两个理由(都是踩过的坑, 见 .workbuddy/memory/2026-09-07.md):
#
#   1. **PATH 坑**: launchd 子进程的 PATH 很短, plist 里直接写 "python3" 会被解析成
#      /usr/bin/python3 —— 本机那个是 3.9, 版本不够(README 要求 3.10+)。
#      这里按固定顺序挑一个**能 import 本项目模块**的解释器, 挑不到就明确报错退出,
#      而不是静默用错版本跑一半。
#   2. **工作目录坑**: 脚本靠 __file__ 定位 BASE, 但必须 cd 到项目目录才能保证
#      数据写到项目 data/ 而不是别处(历史上写丢过一次)。
#
# 想固定解释器: 装 plist 前 export KZZT_PYTHON=/path/to/python3, 或直接改下面数组。
set -u

cd "$(dirname "$0")" || { echo "✗ 进不了项目目录"; exit 1; }

CANDIDATES=(
  "${KZZT_PYTHON:-}"
  /opt/homebrew/bin/python3
  /opt/miniconda3/bin/python3
  /usr/local/bin/python3
  "$(command -v python3 2>/dev/null || true)"
  /usr/bin/python3
)

PY=""
for c in "${CANDIDATES[@]}"; do
  [ -n "$c" ] || continue
  [ -x "$c" ] || continue
  # 必须: 版本 >= 3.10, 且能 import 本项目模块(缺依赖/路径不对的会被这里筛掉)
  if "$c" -c 'import sys, fetch_daily, sector_flow; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "✗ 找不到可用的 python3: 需要 >= 3.10 且能 import fetch_daily / sector_flow"
  echo "  已试过: ${CANDIDATES[*]}"
  echo "  解决: KZZT_PYTHON=/你的/python3 重跑, 或把该路径加进 push_run.sh 的 CANDIDATES"
  exit 1
fi

exec "$PY" push_holdings.py "$@"

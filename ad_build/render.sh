#!/bin/bash
# 用无头 Chrome 把 HTML 渲染成 PNG（Chrome 退出时会卡住，所以后台跑 + 轮询产物）
# 用法: ./render.sh 模板.html 输出.png 宽 高
set -u
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HTML="$1"; OUT="$2"; W="${3:-1200}"; H="${4:-1200}"
DIR="$(cd "$(dirname "$HTML")" && pwd)"
BASE="$(basename "$HTML")"
rm -f "$OUT"

"$CHROME" --headless --disable-gpu --no-sandbox --hide-scrollbars \
  --force-device-scale-factor=1 --default-background-color=00000000 \
  --user-data-dir="$DIR/.chrome" --virtual-time-budget=3000 \
  --window-size="${W},${H}" --screenshot="$OUT" "file://$DIR/$BASE" >/dev/null 2>&1 &
PID=$!

for _ in $(seq 1 100); do
  if [ -s "$OUT" ]; then sleep 1; break; fi     # 等 1s 确认写盘完成
  sleep 0.3
done
kill -9 $PID 2>/dev/null
pkill -9 -f "user-data-dir=$DIR/.chrome" 2>/dev/null

if [ -s "$OUT" ]; then
  echo "OK  $OUT  $(sips -g pixelWidth -g pixelHeight "$OUT" 2>/dev/null | awk '/pixel/{printf "%s ", $2}')"
else
  echo "FAIL $OUT"; exit 1
fi

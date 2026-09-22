#!/bin/bash
# 安装/卸载「每 30 分钟推送持仓到手机」的 launchd 定时任务。
#
# 为什么需要你自己在 Terminal.app 里跑:
#   macOS 26 下 launchctl 在 AI/沙箱会话里 bootstrap 会报 Error 5 (I/O error),
#   crontab 也被拒。所以由这个脚本生成 plist 并加载, 你在自己的终端里执行才有效。
#   (见 .workbuddy/memory/2026-09-07.md)
#
# 用法:
#   bash install_push.sh              # 安装(生成 plist + 加载 + 立刻试跑一次)
#   bash install_push.sh status       # 看当前状态与最近日志
#   bash install_push.sh uninstall    # 卸载
#   bash install_push.sh test         # 手工跑一次(忽略时段门与去重, 直接发到手机)
#   bash install_push.sh dry          # 只预览消息, 不发送
set -uo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.workbuddy.kzzt-push"
PLIST_SRC="$BASE/launchd/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$BASE/data/push.log"
ERR="$BASE/data/push.err"
UID_N="$(id -u)"

gen_plist() {
  mkdir -p "$BASE/launchd"
  # 触发时刻: 9:00~15:30 之间每逢 :00 / :30 醒来(共 14 次)。**具体推不推由脚本的时段门决定**
  # —— 时段判定只有一处真源(sector_flow.session_of), 不在这里再写一遍, 免得两处规则打架。
  # 实测这 14 次里: 9:00/12:00/12:30/15:30 被时段门跳过, 其余 10 次推送 →
  #   9:30 10:00 10:30 11:00 11:30 | 13:00 13:30 14:00 14:30 15:00 (10 条/交易日)
  local cal=""
  for h in 9 10 11 12 13 14 15; do
    for m in 0 30; do
      cal="$cal
        <dict><key>Hour</key><integer>$h</integer><key>Minute</key><integer>$m</integer></dict>"
    done
  done

  cat > "$PLIST_SRC" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$BASE/push_run.sh</string>
    </array>
    <key>WorkingDirectory</key><string>$BASE</string>
    <key>StartCalendarInterval</key>
    <array>$cal
    </array>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$ERR</string>
    <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST
  echo "已生成 $PLIST_SRC"
}

case "${1:-install}" in
  install)
    gen_plist
    mkdir -p "$HOME/Library/LaunchAgents"
    if ! cp "$PLIST_SRC" "$PLIST_DST" 2>/tmp/.kzzt_cp.err; then
      echo "✗ 复制到 $PLIST_DST 失败: $(cat /tmp/.kzzt_cp.err)"
      echo "  → 若是在 AI/沙箱会话里跑的, 沙箱不允许写 ~/Library/LaunchAgents。"
      echo "    请在自己的 Terminal.app 里重跑:  bash \"$BASE/install_push.sh\" install"
      exit 1
    fi
    echo "已复制到 $PLIST_DST"
    launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
    if launchctl bootstrap "gui/$UID_N" "$PLIST_DST" 2>/tmp/.kzzt_boot.err; then
      echo "✓ launchd 已加载"
    else
      echo "✗ launchctl bootstrap 失败: $(cat /tmp/.kzzt_boot.err)"
      echo "  → 常见原因见 .workbuddy/memory/2026-09-07.md: 退出并重新登录一次 macOS,"
      echo "    launchd 会自动扫描 ~/Library/LaunchAgents/ 加载它。"
    fi
    launchctl print "gui/$UID_N/$LABEL" 2>/dev/null | grep -E "state|program|runs" | head -5
    echo
    echo "--- 立刻试跑一次(会走时段门, 非盘中会打印'跳过') ---"
    bash "$BASE/push_run.sh"
    echo
    echo "提示: 没配 ntfy topic 的话先编辑 push_config.json 填 topic,"
    echo "      然后 bash install_push.sh test 发一条到你手机验证通道。"
    ;;
  uninstall)
    launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null && echo "✓ 已卸载 launchd 任务" || echo "(任务本来就没加载)"
    rm -f "$PLIST_DST" && echo "已删除 $PLIST_DST"
    ;;
  status)
    echo "=== launchd ==="
    launchctl print "gui/$UID_N/$LABEL" 2>/dev/null | grep -E "state|last exit|program|runs" || echo "(未加载)"
    echo
    echo "=== 配置文件 ==="
    bash "$BASE/push_run.sh" --config
    echo
    echo "=== 去重状态 ==="
    cat "$BASE/data/push_state.json" 2>/dev/null || echo "(还没有推送记录)"
    echo
    echo "=== 最近日志 (data/push.log) ==="
    tail -20 "$LOG" 2>/dev/null || echo "(暂无)"
    echo
    echo "=== 最近错误 (data/push.err) ==="
    if [ -s "$ERR" ] && grep -q "Operation not permitted" "$ERR"; then
      echo "⚠ 检测到 macOS 隐私保护(TCC)拦截 —— launchd 读不了 ~/Documents 下的项目:"
      tail -4 "$ERR"
      echo
      echo "  这意味着 launchd 这条路当前跑不通(exit 126), 但**推送仍然在工作**:"
      echo "  app.py 里有内置调度(_start_push_scheduler), 只要 app.py 在跑就会按时推。"
      echo "  想让 launchd 也生效, 二选一:"
      echo "    A. 把整个项目移出 ~/Documents(推荐, 例如 mv 到 ~/可转债跟踪), 再重跑本脚本 install"
      echo "    B. 系统设置 -> 隐私与安全性 -> 完全磁盘访问权限, 加入 /bin/bash 与你的 python3"
    else
      tail -10 "$ERR" 2>/dev/null || echo "(暂无)"
    fi
    ;;
  test)   shift; bash "$BASE/push_run.sh" --test "$@" ;;
  dry)    bash "$BASE/push_run.sh" --dry-run --no-state --force ;;
  plist)  gen_plist && plutil -lint "$PLIST_SRC" ;;
  *)      echo "用法: bash install_push.sh [install|uninstall|status|test|dry|plist]"; exit 1 ;;
esac

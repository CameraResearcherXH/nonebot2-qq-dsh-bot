#!/usr/bin/env bash
# scripts/check_live.sh —— 部署机上一键体检（只读，不改任何东西）
#
# 为什么要有（2026-11-26 用户要求）：
#   「ssh 回去看 napcat log 和 docker bot log，看看数据是否 ok；顺便检测全量和节能。」
#   平时要敲 6~7 条命令、还得记得 grep 哪些关键词，容易漏。
#
# 用法（在部署机 /home/admin/qq-grok-bot 下）：
#   bash scripts/check_live.sh              # 默认看最近 6 小时
#   bash scripts/check_live.sh 24           # 看最近 24 小时
#   HOURS=12 bash scripts/check_live.sh
#
# 说明：
#   · .env 是 root 所有 → docker compose 需要 sudo；用 SUDO="" 可关掉（如果你的账号在 docker 组且能读 .env）
#   · 只读：不重启、不改 .env、不写业务数据（daily_report 只写 data/reports/daily/）
set -u

cd "$(dirname "$0")/.." 2>/dev/null || cd /home/admin/qq-grok-bot || exit 1
HOURS="${1:-${HOURS:-6}}"
SUDO="${SUDO-sudo -n}"
BOT="${BOT_NAME:-qq-grok-bot}"
NAP="${NAPCAT_NAME:-napcat}"

hr() { printf '\n\033[1m==== %s ====\033[0m\n' "$*"; }

hr "容器状态"
$SUDO docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

hr "代码版本（容器里那份是不是最新的）"
$SUDO docker compose exec -T bot python scripts/verify_deploy.py 2>&1 | head -8

hr "bot 日志：最近 ${HOURS}h 关键行（好感 / 锚点 / 判读 / 归因）"
$SUDO docker logs --since "${HOURS}h" "$BOT" 2>&1 \
  | grep -aE "AFFv2|好感v1|好感v2|\[ANCHOR\]|MOOD-JUDGE|\[ADDR\]|\[ENV\]" \
  | tail -80

hr "bot 日志：报错统计（最近 ${HOURS}h）"
_tb=$($SUDO docker logs --since "${HOURS}h" "$BOT" 2>&1 | grep -ac "Traceback")
_er=$($SUDO docker logs --since "${HOURS}h" "$BOT" 2>&1 | grep -acE "ERROR|CRITICAL")
echo "Traceback: ${_tb} 次    ERROR/CRITICAL: ${_er} 次"
if [ "${_tb}" -gt 0 ] || [ "${_er}" -gt 0 ]; then
  echo "---- 最后 5 条报错 ----"
  $SUDO docker logs --since "${HOURS}h" "$BOT" 2>&1 \
    | grep -aE "Traceback|ERROR|CRITICAL" | tail -5
fi

hr "bot 日志：启动体检 + 判读采样（最后 5 条）"
$SUDO docker logs --since "${HOURS}h" "$BOT" 2>&1 \
  | grep -aE "启动配置体检|\[MOOD-JUDGE\]|\[AFFv2\]" | tail -5

hr "napcat 日志：最近 2h 尾 60 行"
$SUDO docker logs --since 2h "$NAP" 2>&1 | tail -60

hr "napcat 日志：掉线/登录/错误（最近 ${HOURS}h）"
$SUDO docker logs --since "${HOURS}h" "$NAP" 2>&1 \
  | grep -aiE "offline|disconnect|kicked|登录|重新连接|error|fail" | tail -25

hr "数值体检（心情 / 好感 / 判读链路 / 今日事件 / 成本 / 期望校验）"
$SUDO docker compose exec -T bot python scripts/daily_report.py --top 10

hr "回滚备忘（数字不对时才用）"
cat <<'TIP'
  · 好感模型回滚：.env 里 AFF_MODEL=v1（本来就是默认），改完 docker compose restart bot
  · 心情链路回滚：.env 里 MOOD_UPDATE=delta（退回「累加 Δ」，锚点不生效）
  · 只想降锚点权重：MOOD_ANCHOR_WEIGHT=0.25（默认 0.45）
  · 改完记得看：docker logs -f --tail 200 qq-grok-bot | grep -aE "ANCHOR|好感|MOOD-JUDGE"
TIP

#!/usr/bin/env bash
# NapCat 掉线看门狗（2026-11-24）
#
# 用户问：「QQ 掉线能不能自己登录？包括重启 NapCat。」
#
# 分工：
#   · 容器挂了 / 退出      → docker start napcat（restart:always 之外的兜底）
#   · 登录失效 / 二维码    → 只有在 .env 配了 NAPCAT_QUICK_PASSWORD 或
#                            NAPCAT_QUICK_PASSWORD_MD5 时才 docker restart napcat，
#                            让它走「密码快速登录」；没配密码就只记日志、不反复重启
#                            （否则二维码会被反复作废，反而更麻烦）。
#   · 冷却 10 分钟，避免登录失败时死循环重启。
#
# 安装（服务器上，admin 用户）：
#   chmod +x ~/qq-grok-bot/scripts/napcat_watchdog.sh
#   crontab -e
#   * * * * * /home/admin/qq-grok-bot/scripts/napcat_watchdog.sh
#
# 日志：data/napcat_watchdog.log
# 状态：data/.napcat_watchdog_state（上次重启时间）
set -u

ROOT="${QQBOT_ROOT:-/home/admin/qq-grok-bot}"
LOG="$ROOT/data/napcat_watchdog.log"
STATE="$ROOT/data/.napcat_watchdog_state"
ENV_FILE="$ROOT/.env"
COOLDOWN="${NAPCAT_WATCHDOG_COOLDOWN:-600}"

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
log() { echo "[$(date '+%F %T')] $*" >>"$LOG" 2>&1; }

docker_ok=0
if command -v docker >/dev/null 2>&1; then
  docker_ok=1
fi
if [ "$docker_ok" != "1" ]; then
  log "找不到 docker，退出"
  exit 0
fi

# 1) 容器在不在
running="$(sudo docker inspect -f '{{.State.Running}}' napcat 2>/dev/null || echo false)"
if [ "$running" != "true" ]; then
  log "napcat 容器没在跑 → docker start napcat"
  sudo docker start napcat >>"$LOG" 2>&1 || true
  date +%s >"$STATE" 2>/dev/null || true
  exit 0
fi

# 2) 最近日志：有收发就是活着，不用管
tail_log="$(sudo docker logs --tail 250 napcat 2>&1 || true)"
if echo "$tail_log" | grep -qE '接收 <-|发送 ->'; then
  rm -f "$ROOT/data/.napcat_login_failed" 2>/dev/null || true
  exit 0
fi

# 3) 有没有登录失败 / 等扫码
if ! echo "$tail_log" | grep -qE '快速登录错误|Login Error|请扫描下面的二维码|账号已离线|KickedOffLine|登录服务已断开'; then
  exit 0
fi
touch "$ROOT/data/.napcat_login_failed" 2>/dev/null || true

# 3.5) 密码登录被腾讯要求短信/验证码 → 只能人工完成，别反复重启
#      （否则每 10 分钟换一次 captcha，用户永远追不上）
if echo "$tail_log" | grep -qE '需要验证码|验证码|proofWaterUrl|captcha|sms-verify'; then
  log "NapCat 密码登录需要人工验证码/短信 → 请在 WebUI 完成，不自动重启"
  exit 0
fi

# 4) 只有配了密码才自动重启（否则重启只会换一张二维码，仍要人扫）
pass_set=0
if sudo grep -qE '^NAPCAT_QUICK_PASSWORD=.+' "$ENV_FILE" 2>/dev/null; then pass_set=1; fi
if sudo grep -qE '^NAPCAT_QUICK_PASSWORD_MD5=.+' "$ENV_FILE" 2>/dev/null; then pass_set=1; fi
if [ "$pass_set" != "1" ]; then
  log "NapCat 掉线，但 .env 没配 NAPCAT_QUICK_PASSWORD / _MD5 → 需要扫码，不自动重启"
  exit 0
fi

# 5) 冷却
now="$(date +%s)"
last="$(cat "$STATE" 2>/dev/null || echo 0)"
case "$last" in (*[!0-9]*|'') last=0 ;; esac
if [ $((now - last)) -lt "$COOLDOWN" ]; then
  log "NapCat 掉线，冷却中（$((COOLDOWN - (now - last)))s）→ 暂不重启"
  exit 0
fi

log "NapCat 掉线且已配密码 → docker restart napcat（尝试自动登录）"
sudo docker restart napcat >>"$LOG" 2>&1 || true
date +%s >"$STATE" 2>/dev/null || true

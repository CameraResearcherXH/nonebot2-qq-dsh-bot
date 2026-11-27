#!/usr/bin/env bash
# ============================================================
# Docker 清理 + 重建脚本（安全版）
#
# 原则：
#   · 不动 docker volume（项目数据在 bind mount 的 ./data ./napcat，不受影响）
#   · 不加 -v，不删 compose 定义的任何东西
#   · 默认 dry-run，只有加 --yes 才真删
#
# 用法（在服务器 ~/qq-grok-bot 下）：
#   bash docker_cleanup.sh                 # 只看，不删
#   sudo bash docker_cleanup.sh --yes      # 真清（日志 truncate 需要 root）
#   sudo bash docker_cleanup.sh --yes --rebuild   # 清完顺便重建
#
# ⚠️ 从 Windows 传上来先转一下换行符，否则会报 $'\r': command not found：
#   sed -i 's/\r$//' docker_cleanup.sh
# ============================================================
set -uo pipefail

YES=0
REBUILD=0
for a in "$@"; do
    case "$a" in
        --yes|-y) YES=1 ;;
        --rebuild) REBUILD=1 ;;
    esac
done

say() { printf '%s\n' "$*"; }

run() {
    if [ "$YES" -eq 1 ]; then
        say "+ $*"
        "$@" || say "  ⚠️ 上面这条失败了，继续"
    else
        say "[dry-run] $*"
    fi
}

say "============================================================"
say " 清理前"
say "============================================================"
df -h / 2>/dev/null || true
say ""
docker system df 2>/dev/null || true

say ""
say "============================================================"
say " 1. 容器日志"
say "============================================================"
if [ "$(id -u)" -eq 0 ]; then
    say "当前日志文件（前 5 大）："
    du -sh /var/lib/docker/containers/*/*-json.log 2>/dev/null | sort -h | tail -5 || say "  （没有找到）"
    if [ "$YES" -eq 1 ]; then
        find /var/lib/docker/containers -name '*-json.log' -exec truncate -s 0 {} \; 2>/dev/null
        say "✅ 已清空所有容器日志（truncate，不是 rm）"
    else
        say "[dry-run] find /var/lib/docker/containers -name '*-json.log' -exec truncate -s 0 {} \;"
    fi
else
    say "⚠️ 不是 root，跳过。手动执行："
    say "   sudo sh -c 'truncate -s 0 /var/lib/docker/containers/*/*-json.log'"
fi

say ""
say "============================================================"
say " 2. 构建缓存（通常最大的一块）"
say "============================================================"
run docker builder prune -af

say ""
say "============================================================"
say " 3. 悬空镜像 / 已停止容器 / 无用网络"
say "============================================================"
run docker image prune -f
run docker container prune -f
run docker network prune -f

say ""
say "⚠️ 没有动 docker volume，也没有用 -v"
say "   项目数据在 bind mount：./data（记忆/表情/临时音频）、./napcat/qq（QQ 登录态）"

if [ "$REBUILD" -eq 1 ]; then
    say ""
    say "============================================================"
    say " 4. 重建"
    say "============================================================"
    run docker compose down
    run docker compose build --no-cache
    run docker compose up -d
    say ""
    say "等 10 秒后看启动日志："
    sleep 10
    docker compose logs --tail 40 2>/dev/null || true
    say ""
    say "确认日志轮转已生效（应该看到 max-size 20m / max-file 3）："
    docker inspect qq-grok-bot --format '{{json .HostConfig.LogConfig}}' 2>/dev/null || true
    say ""
    say "重建后再清一次旧镜像："
    run docker image prune -f
else
    say ""
    say "（没加 --rebuild，跳过重建。要重建就再加 --rebuild）"
fi

say ""
say "============================================================"
say " 清理后"
say "============================================================"
df -h / 2>/dev/null || true
say ""
docker system df 2>/dev/null || true

say ""
say "提示：项目自己的临时音频也可以清"
say "  du -sh data/temp_audio"
say "  rm -f data/temp_audio/*.wav data/temp_audio/*.mp3"


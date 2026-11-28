#!/usr/bin/env bash
# ============================================================================
# find_napcat_mirror.sh —— 找一个「还缓存着 NapCat 镜像」的 Docker 源
#
# 背景（2026-11-28）：
#   mlikiowa/napcat-docker 已从 Docker Hub 消失（NapNeko/NapCat-Docker 仓库 404）。
#   各加速站只保证「曾经缓存过」的 tag，latest 往往没有，必须逐个版本号探。
#
# 用法：
#   bash scripts/find_napcat_mirror.sh                 # 只探测
#   bash scripts/find_napcat_mirror.sh --write-env     # 探到就写进仓库根目录的 .env
#   bash scripts/find_napcat_mirror.sh --pull          # 拉下来 + 打本地 tag + 导出 tar
#   bash scripts/find_napcat_mirror.sh --pull --write-env
#   TAGS="v4.18.3 v4.17.45" bash scripts/find_napcat_mirror.sh
#   MIRRORS="docker.m.daocloud.io" bash scripts/find_napcat_mirror.sh
#
# 为什么不用 `docker manifest inspect`：
#   它**没有超时**，源被墙/黑洞时会挂几分钟（Windows 版就卡在这），
#   而且 CLI 会去连 auth.docker.io —— 那台机器上根本连不上（实测 rc=28）。
#
# 为什么不用裸 `curl -f`：
#   这些源都会先回 401 + WWW-Authenticate，裸 curl 会把「有镜像」误判成「没有」。
#   所以这里走完整的 401 → 取 token → 再取 manifest。
#
# ⚠️ 为什么写 .env 而不是 docker-compose.yml：
#    compose 里是 ${NAPCAT_IMAGE:-默认值}，把某个第三方加速站写死进 compose，
#    等于让所有用户都依赖那一个源 —— 上游一删库就又是今天这个局面。
#    .env 是每个部署自己的、不进仓库，换源只改一行。
#
# ⚠️ 本地打的 napcat-local:* 只是**备用**，不进仓库、不推 registry。
#    给用户的是上面探到的**源地址**。
# ============================================================================
set -uo pipefail

IMAGE="${IMAGE:-mlikiowa/napcat-docker}"
TAGS="${TAGS:-latest v4.19.0 v4.18.3 v4.18.0 v4.17.45 v4.17.0 v4.16.1 v4.15.0}"
# daocloud 放第一个：2026-11-28 实测只有它还缓存着这个镜像
MIRRORS="${MIRRORS:-docker.m.daocloud.io docker.1ms.run docker.xuanyuan.me dockerproxy.net docker.1panel.live hub.rat.dev}"

DO_PULL=0
WRITE_ENV=0
for a in "$@"; do
  case "$a" in
    --pull)      DO_PULL=1 ;;
    --write-env) WRITE_ENV=1 ;;
    -h|--help)   sed -n '2,34p' "$0"; exit 0 ;;
    *) echo "未知参数：$a（可用：--pull / --write-env）" >&2; exit 1 ;;
  esac
done

echo "镜像: $IMAGE"
echo "源  : $MIRRORS"
echo "tag : $TAGS"
echo "------------------------------------------------------------------"

if ! command -v curl >/dev/null 2>&1; then
  echo "❌ 需要 curl" >&2
  exit 1
fi

ACCEPT="application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json"

# 返回 http code：200=有  404=这个 tag 没有（源活着）  000/5xx=源不可达  403=源拒绝
manifest_code() {  # $1=mirror $2=tag
  local m="$1" t="$2" url hdr code realm svc scp tok
  url="https://$m/v2/$IMAGE/manifests/$t"
  hdr=$(curl -s -L -m 6 -D - -o /dev/null -H "Accept: $ACCEPT" "$url" 2>/dev/null) || { echo 000; return; }
  code=$(printf '%s' "$hdr" | tr -d '\r' | grep -i '^HTTP/' | tail -1 | awk '{print $2}')
  [ -z "$code" ] && code=000
  [ "$code" != "401" ] && { echo "$code"; return; }

  local auth
  auth=$(printf '%s' "$hdr" | tr -d '\r' | grep -i '^www-authenticate:' | tail -1)
  realm=$(printf '%s' "$auth" | sed -n 's/.*realm="\([^"]*\)".*/\1/p')
  svc=$(printf '%s' "$auth" | sed -n 's/.*service="\([^"]*\)".*/\1/p')
  scp=$(printf '%s' "$auth" | sed -n 's/.*scope="\([^"]*\)".*/\1/p')
  [ -z "$realm" ] && { echo 403; return; }
  # 有的源（如 docker.m.daocloud.io）401 里**不带 scope**，得自己补，
  # 否则 token 请求拿不到 → 误判成「源不可达」。
  [ -z "$scp" ] && scp="repository:$IMAGE:pull"
  local sep="?"
  case "$realm" in *\?*) sep="&" ;; esac
  local tok_url="$realm${sep}service=$svc&scope=$scp"

  tok=$(curl -s -m 6 "$tok_url" 2>/dev/null | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
  [ -z "$tok" ] && tok=$(curl -s -m 6 "$tok_url" 2>/dev/null \
        | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
  [ -z "$tok" ] && { echo 403; return; }

  code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $tok" -H "Accept: $ACCEPT" "$url" 2>/dev/null) || code=000
  echo "$code"
}

FOUND=()
for m in $MIRRORS; do
  for t in $TAGS; do
    c=$(manifest_code "$m" "$t")
    case "$c" in
      200)
        echo "✅ $m/$IMAGE:$t"
        FOUND+=("$m/$IMAGE:$t")
        break ;;                      # 同一个源只取第一个可用 tag
      404|401)
        continue ;;                   # 源活着，这个 tag 没有，试下一个
      *)
        echo "⚠️  $m 不可达/拒绝（$c），跳过它剩下的 tag"
        break ;;
    esac
  done
done

echo "------------------------------------------------------------------"
if [ ${#FOUND[@]} -eq 0 ]; then
  echo "❌ 全部源都没探到。三个选择："
  echo "   1) 加大 TAGS 范围（问群里现在跑的是哪个版本：docker images | grep napcat）"
  echo "   2) 从还在跑的机器上 docker save 一份，docker load 过来（最稳）"
  echo "   3) 换协议端：lagrangedev/lagrange.core"
  exit 2
fi

echo "可用："
printf '  %s\n' "${FOUND[@]}"
echo
echo "→ 写进 .env（挑一个）："
for ref in "${FOUND[@]}"; do echo "   NAPCAT_IMAGE=$ref"; done

PICK="${FOUND[0]}"

if [ "$DO_PULL" -eq 1 ]; then
  TAG="${PICK##*:}"
  echo
  echo "== 拉取 $PICK =="
  docker pull "$PICK" || exit 3
  echo "== 打本地备用 tag =="
  docker tag "$PICK" "napcat-local:$TAG"
  echo "== 导出离线包 ~/napcat-image.tar =="
  if docker save -o "$HOME/napcat-image.tar" "napcat-local:$TAG"; then
    echo "✅ 已导出：$HOME/napcat-image.tar（换机器 docker load -i 即可）"
  fi
  echo
  echo "本机 .env 建议写：NAPCAT_IMAGE=napcat-local:$TAG（与上游彻底脱钩）"
  PICK="napcat-local:$TAG"
fi

if [ "$WRITE_ENV" -eq 1 ]; then
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
  ENVF="$ROOT/.env"
  if [ ! -f "$ENVF" ]; then
    echo "❌ 没找到 $ENVF（先 cp .env.example .env）" >&2
    exit 4
  fi
  cp "$ENVF" "$ENVF.bak.$(date +%Y%m%d%H%M%S)"
  grep -v '^NAPCAT_IMAGE=' "$ENVF" > "$ENVF.tmp" || true
  printf 'NAPCAT_IMAGE=%s\n' "$PICK" >> "$ENVF.tmp"
  mv "$ENVF.tmp" "$ENVF"
  echo "✅ 已写入 $ENVF：NAPCAT_IMAGE=$PICK（原文件已备份 .bak.*）"
  echo "   生效：docker compose up -d --force-recreate napcat（会重新登录，需扫码）"
fi

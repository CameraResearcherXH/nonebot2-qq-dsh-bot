#!/bin/bash
# 在香港服务器上跑：把 4 个 LoRA 上传到 GitHub Release，供算力机走 VPN 拉。
# GitHub Release 附件单文件上限 2GB，超过 100MB 的 LoRA 必须走 release，不能直接 push 仓库。
#
# 用法：
#   GITHUB_TOKEN=ghp_xxx REPO=你的用户名/仓库名 bash upload_loras_to_github.sh
#   （REPO 可以是私有仓库；token 需要 repo 权限）
set -e

TOKEN="${GITHUB_TOKEN:?请设置 GITHUB_TOKEN}"
REPO="${REPO:?请设置 REPO，格式 owner/repo}"
TAG="loras-$(date +%s)"
LORAS_DIR="$HOME/qq-grok-bot/loras"
FILES=(
  AddMicroDetails_NoobAI.safetensors
  ThickerLinesAnimeStyle.safetensors
  FlatColor_Illustrious.safetensors
  Nnegrat_2_5D_High.safetensors
)

echo "== 确保仓库非空（空仓库不能建 release，先写入 README）"
curl -sS -X PUT \
  -H "Authorization: token $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/contents/README.md" \
  -d '{"message":"init","content":"IyBMb3JhIHRlbXBvcmFyeSBzdG9yYWdlCg=="}' > /dev/null

echo "== 创建 release: $TAG"
resp=$(curl -sS -X POST \
  -H "Authorization: token $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/releases" \
  -d "{\"tag_name\":\"$TAG\",\"name\":\"$TAG\",\"draft\":false}")

upload_url=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('upload_url','').split('{')[0])" 2>/dev/null || echo "")
if [ -z "$upload_url" ]; then
  echo "❌ 创建 release 失败，GitHub 返回："
  echo "$resp" | head -c 800
  echo ""
  exit 1
fi

for f in "${FILES[@]}"; do
  echo "== 上传 $f ..."
  curl -sS -X POST \
    -H "Authorization: token $TOKEN" \
    -H "Content-Type: application/octet-stream" \
    --data-binary "@$LORAS_DIR/$f" \
    "$upload_url?name=$f" > /dev/null
  echo "   done"
done

echo ""
echo "✅ 上传完成，算力机下载地址："
for f in "${FILES[@]}"; do
  echo "  https://github.com/$REPO/releases/download/$TAG/$f"
done

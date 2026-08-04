#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

patterns=(
  'OA_APP_KEY=app_[0-9a-fA-F]{32}'
  'OA_APP_SECRET=[A-Za-z0-9+/]{24,}={0,2}'
  'OA_SSO_SECRET=[A-Za-z0-9+/]{32,}={0,2}'
  'DASHSCOPE_API_KEY=sk-[A-Za-z0-9_-]{16,}'
  'api[_-]?key["'"'']?[[:space:]]*[:=][[:space:]]*["'"'']sk-[A-Za-z0-9_-]{16,}'
)

failed=0
for pattern in "${patterns[@]}"; do
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    matches="$(git grep -n -E "$pattern" -- ':!scripts/check-secrets.sh' 2>/dev/null \
      | grep -Eiv 'placeholder|example|xxxxxxxx' || true)"
  else
    matches="$(grep -RInIE \
      --exclude='check-secrets.sh' \
      --exclude='.env' \
      --exclude='.env.sha256' \
      "$pattern" . 2>/dev/null \
      | grep -Eiv 'placeholder|example|xxxxxxxx' || true)"
  fi
  if [ -n "$matches" ]; then
    printf '%s\n' "$matches" | cut -d: -f1 | sort -u
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "检测到疑似真实凭据；请从仓库移除并改为受控环境变量。OA 三项既有凭据保持原值。" >&2
  exit 1
fi

echo "未在代码文件中发现已知格式的明文凭据。"

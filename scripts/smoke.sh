#!/bin/sh
# 对跑起来的 docker compose 做冒烟测试：建公司、登录、建工单、传证据、再下载比对。
set -eu

BASE=http://localhost:${DESK_HTTP_PORT:-8080}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
JAR=$WORK/cookies

for _ in $(seq 1 60); do
  # 还没登录时 401 说明 Nginx 和 API 都通了
  [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/auth/me" || true)" = 401 ] && break
  sleep 2
done
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/auth/me")" = 401 ]

docker compose exec -T \
  -e DESK_MIGRATION_DATABASE_URL="postgresql+asyncpg://desk_owner:$POSTGRES_PASSWORD@db/exception_desk" \
  -e DESK_BOOTSTRAP_PASSWORD="$DESK_BOOTSTRAP_PASSWORD" \
  api python -m scripts.create_tenant \
  --slug smoke --name 冒烟测试 --email admin@smoke.example.com --admin-name 管理员

curl -sf -c "$JAR" -H 'Content-Type: application/json' \
  -d "{\"tenant_slug\":\"smoke\",\"email\":\"admin@smoke.example.com\",\"password\":\"$DESK_BOOTSTRAP_PASSWORD\"}" \
  "$BASE/api/auth/login" >/dev/null

TICKET=$(curl -sf -b "$JAR" -H 'Content-Type: application/json' \
  -d '{"type":"damaged","title":"冒烟测试工单"}' "$BASE/api/tickets" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# 6 MB 的文件：确认 Nginx 的上传限制放开了，默认只有 1 MB
{ printf '\211PNG\r\n\032\n'; head -c 6291456 /dev/urandom; } > "$WORK/photo.png"
ATTACHMENT=$(curl -sf -b "$JAR" --data-binary "@$WORK/photo.png" \
  -H 'Content-Type: image/png' \
  "$BASE/api/tickets/$TICKET/attachments?category=package_photo&filename=photo.png" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["attachments"][0]["id"])')

curl -sf -b "$JAR" -o "$WORK/back.png" "$BASE/api/tickets/$TICKET/attachments/$ATTACHMENT/content"
cmp "$WORK/photo.png" "$WORK/back.png"

# 重启后文件还在：确认放在存储卷里，不在容器里
docker compose up -d --force-recreate api
for _ in $(seq 1 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$BASE/api/auth/me" || true)" = 200 ] && break
  sleep 2
done
curl -sf -b "$JAR" -o "$WORK/again.png" "$BASE/api/tickets/$TICKET/attachments/$ATTACHMENT/content"
cmp "$WORK/photo.png" "$WORK/again.png"

echo "smoke test passed"

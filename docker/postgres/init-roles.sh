#!/bin/sh
# 只在数据库首次初始化时执行。创建运行时使用的受限角色：没有 SUPERUSER 和 BYPASSRLS。
set -eu

psql -v ON_ERROR_STOP=1 -v app_password="$DESK_APP_PASSWORD" \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE desk_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'app_password';
SQL

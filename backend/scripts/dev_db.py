"""不装 Docker 也能开发：启动内置的 PostgreSQL，并在第一次运行时生成 .env。

uv run python -m scripts.dev_db
"""

import secrets
from pathlib import Path

import pgserver

BACKEND = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND / ".devdb"
ENV_FILE = BACKEND / ".env"
DB_NAME = "exception_desk"


def ensure_role(server, name: str) -> None:
    server.psql(
        f"""DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{name}') THEN
            CREATE ROLE {name} LOGIN;
          END IF;
        END $$;"""
    )


def main() -> None:
    # cleanup_mode=None：脚本退出后数据库继续运行，供接下来的命令和开发服务使用
    server = pgserver.get_server(DATA_DIR, cleanup_mode=None)
    ensure_role(server, "desk_owner")
    ensure_role(server, "desk_app")
    if DB_NAME not in server.psql("SELECT datname FROM pg_database;"):
        server.psql(f"CREATE DATABASE {DB_NAME} OWNER desk_owner;")

    host_port = server.get_uri().split("@", 1)[1].split("/", 1)[0]
    if not ENV_FILE.exists():
        ENV_FILE.write_text(
            f"DESK_DATABASE_URL=postgresql+asyncpg://desk_app@{host_port}/{DB_NAME}\n"
            f"DESK_MIGRATION_DATABASE_URL=postgresql+asyncpg://desk_owner@{host_port}/{DB_NAME}\n"
            f"DESK_JWT_SECRET={secrets.token_urlsafe(48)}\n"
            "DESK_COOKIE_SECURE=false\n"
            "DESK_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173\n",
            encoding="utf-8",
        )
        print(f"已生成 {ENV_FILE}")
    print(f"PostgreSQL 已启动：{host_port}/{DB_NAME}")


if __name__ == "__main__":
    main()

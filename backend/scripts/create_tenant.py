"""创建公司和第一位管理员。用法：

uv run python -m scripts.create_tenant --slug acme --name 演示物流 \
    --email admin@acme.example.com --admin-name 管理员

密码从环境变量 DESK_BOOTSTRAP_PASSWORD 读取，不走命令行参数，避免留在 shell 历史里。
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine

from app.bootstrap import create_tenant_with_admin


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--admin-name", required=True)
    parser.add_argument("--role", default="admin")
    args = parser.parse_args()

    load_dotenv()
    password = os.environ["DESK_BOOTSTRAP_PASSWORD"]
    if len(password) < 10:
        raise SystemExit("DESK_BOOTSTRAP_PASSWORD 至少需要 10 位")

    engine = create_async_engine(os.environ["DESK_MIGRATION_DATABASE_URL"])
    async with engine.begin() as connection:
        await create_tenant_with_admin(
            connection,
            slug=args.slug,
            name=args.name,
            admin_email=args.email,
            admin_name=args.admin_name,
            admin_password=password,
            role=args.role,
        )
    await engine.dispose()
    print(f"已创建公司 {args.slug}，管理员 {args.email}")


if __name__ == "__main__":
    asyncio.run(main())

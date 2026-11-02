from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langchain_openai import ChatOpenAI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.routers import ai, attachments, auth, claims, members, tickets

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await engine.dispose()

    app = FastAPI(title="异常件协同台", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    # commit 后不让对象过期：提交后 RLS 的租户设置已失效，不能再回库刷新
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    app.state.chat_model = None
    if settings.llm_api_key and settings.llm_model:
        app.state.chat_model = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url or None,
            timeout=settings.llm_timeout_seconds,
            max_retries=1,
            temperature=0.2,
        )

    @app.middleware("http")
    async def reject_cross_origin_writes(request: Request, call_next):
        # 登录态放在 Cookie 里，这里配合 SameSite=Lax 防 CSRF。
        # 浏览器的跨站写请求都会带 Origin；没有 Origin 的不是浏览器请求，也不会自动带 Cookie。
        origin = request.headers.get("origin")
        if (
            request.method not in _SAFE_METHODS
            and origin is not None
            and origin.rstrip("/") not in settings.origins
        ):
            return JSONResponse({"detail": "不允许的请求来源"}, status_code=403)
        return await call_next(request)

    @app.get("/health", tags=["system"])
    async def health():
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError):
            return JSONResponse({"status": "database_unavailable"}, status_code=503)
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(members.router)
    app.include_router(tickets.router)
    app.include_router(attachments.router)
    app.include_router(claims.router)
    app.include_router(ai.router)
    return app

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import Settings

COOKIE_NAME = "desk_session"
_ALGORITHM = "HS256"

_hasher = PasswordHasher()
# 账号不存在时也校验一次，避免通过响应时间探测邮箱是否已注册
_DUMMY_HASH = _hasher.hash("timing-equalizer")


@dataclass(frozen=True)
class SessionClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    session_version: int


async def hash_password(password: str) -> str:
    # Argon2 故意很慢，放到线程里执行，不阻塞事件循环
    return await asyncio.to_thread(_hasher.hash, password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def verify_password(password_hash: str | None, password: str) -> bool:
    if password_hash is None:
        await asyncio.to_thread(_verify, _DUMMY_HASH, password)
        return False
    return await asyncio.to_thread(_verify, password_hash, password)


def create_session_token(claims: SessionClaims, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(claims.user_id),
        "tid": str(claims.tenant_id),
        "ver": claims.session_version,
        "iat": now,
        "exp": now + timedelta(minutes=settings.session_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=_ALGORITHM)


def decode_session_token(token: str, settings: Settings) -> SessionClaims | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
            options={"require": ["exp", "sub", "tid", "ver"]},
        )
        return SessionClaims(
            user_id=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tid"]),
            session_version=int(payload["ver"]),
        )
    except (jwt.InvalidTokenError, ValueError, TypeError):
        return None

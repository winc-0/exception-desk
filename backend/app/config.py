from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DESK_", extra="ignore")

    # 必须使用受限的应用角色 desk_app。超级用户和 BYPASSRLS 角色不受 RLS 约束。
    database_url: str
    jwt_secret: SecretStr = Field(min_length=32)
    session_ttl_minutes: int = 480
    cookie_secure: bool = True
    allowed_origins: str = ""
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    attachment_dir: str = "./data/attachments"

    @property
    def origins(self) -> set[str]:
        return {o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()

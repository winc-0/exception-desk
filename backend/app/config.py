from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
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
    # OpenAI 兼容接口。不配置时 AI 回复草稿退回到模板，其他功能不受影响
    llm_base_url: str = ""
    llm_api_key: SecretStr | None = None
    llm_model: str = ""
    llm_timeout_seconds: float = 30
    ai_hourly_limit: int = 30

    @field_validator("llm_api_key", mode="before")
    @classmethod
    def blank_key_means_none(cls, value):
        # docker compose 没填时传的是空字符串
        return value or None

    @property
    def origins(self) -> set[str]:
        return {o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()

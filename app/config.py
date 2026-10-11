from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://callback:callback@localhost:5432/callback"
    )  # MVP default only
    allowed_hosts: list[str] = Field(default_factory=lambda: ["example.com"])
    request_timeout_seconds: float = 10.0
    lease_seconds: int = 30
    max_attempts: int = 8
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 3600.0
    worker_poll_seconds: float = 1.0
    log_level: str = Field(default="INFO")

    @model_validator(mode="after")
    def validate_worker_limits(self) -> "Settings":
        if self.request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        if self.lease_seconds <= self.request_timeout_seconds:
            raise ValueError("lease must be longer than the HTTP timeout")
        if self.max_attempts < 1:
            raise ValueError("max attempts must be at least one")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

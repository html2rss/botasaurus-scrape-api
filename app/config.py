"""Centralized runtime configuration loaded from environment variables."""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_env_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "on"}


class LogFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class SentrySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    dsn: str = Field(default="", validation_alias="SENTRY_DSN")
    environment: str = Field(default="", validation_alias="SENTRY_ENVIRONMENT")
    release: str = Field(default="", validation_alias="SENTRY_RELEASE")
    send_default_pii: bool = Field(
        default=False, validation_alias="SENTRY_SEND_DEFAULT_PII"
    )

    @field_validator("send_default_pii", mode="before")
    @classmethod
    def parse_bool(cls, value: object) -> bool:
        return _parse_env_bool(value)

    def effective_environment(self, deployment_environment: str) -> str:
        return (self.environment or deployment_environment or "production").strip()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    scrape_timeout_seconds: int = Field(
        default=45, validation_alias="SCRAPE_TIMEOUT_SECONDS"
    )
    scrape_work_timeout_seconds: int = Field(
        default=30, validation_alias="SCRAPE_WORK_TIMEOUT_SECONDS"
    )
    scrape_max_workers: int = Field(default=4, validation_alias="SCRAPE_MAX_WORKERS")
    scrape_max_per_host: int = Field(
        default=2, ge=1, validation_alias="SCRAPE_MAX_PER_HOST"
    )
    scrape_runtime_min_free_bytes: int = Field(
        default=256 * 1024 * 1024,
        validation_alias="SCRAPE_RUNTIME_MIN_FREE_BYTES",
    )
    scrape_prewarm: bool = Field(default=False, validation_alias="SCRAPE_PREWARM")
    scrape_prewarm_idle_ttl_seconds: int = Field(
        default=600,
        ge=0,
        validation_alias="SCRAPE_PREWARM_IDLE_TTL_SECONDS",
    )
    scrape_prewarm_min_refill_seconds: int = Field(
        default=30,
        ge=0,
        validation_alias="SCRAPE_PREWARM_MIN_REFILL_SECONDS",
    )
    scrape_runtime_stats_interval_seconds: int = Field(
        default=60,
        ge=0,
        validation_alias="SCRAPE_RUNTIME_STATS_INTERVAL_SECONDS",
    )
    log_format: LogFormat = Field(default=LogFormat.TEXT, validation_alias="LOG_FORMAT")
    runtime_root: Path = Field(default=Path("/tmp/scrape"))
    environment: str = Field(default="production", validation_alias="ENVIRONMENT")
    sentry: SentrySettings = Field(default_factory=SentrySettings)

    @field_validator("scrape_prewarm", mode="before")
    @classmethod
    def parse_scrape_prewarm(cls, value: object) -> bool:
        return _parse_env_bool(value)

    @field_validator("log_format", mode="before")
    @classmethod
    def parse_log_format(cls, value: object) -> LogFormat:
        if isinstance(value, LogFormat):
            return value
        raw = str(value or LogFormat.TEXT).strip().lower()
        try:
            return LogFormat(raw)
        except ValueError:
            return LogFormat.TEXT

    @model_validator(mode="after")
    def validate_timeout_relationship(self) -> Settings:
        if self.scrape_work_timeout_seconds > self.scrape_timeout_seconds:
            raise ValueError(
                "SCRAPE_WORK_TIMEOUT_SECONDS cannot exceed SCRAPE_TIMEOUT_SECONDS: "
                f"work={self.scrape_work_timeout_seconds} "
                f"total={self.scrape_timeout_seconds}"
            )
        return self

    @property
    def default_wait_timeout_seconds(self) -> int:
        return min(15, self.scrape_work_timeout_seconds)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()

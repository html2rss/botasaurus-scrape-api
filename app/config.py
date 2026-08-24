"""Centralized runtime configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SentrySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    dsn: str = Field(default="", validation_alias="SENTRY_DSN")
    environment: str = Field(default="", validation_alias="SENTRY_ENVIRONMENT")
    release: str = Field(default="", validation_alias="SENTRY_RELEASE")
    traces_sample_rate: float = Field(
        default=0.0, validation_alias="SENTRY_TRACES_SAMPLE_RATE"
    )
    profiles_sample_rate: float = Field(
        default=0.0, validation_alias="SENTRY_PROFILES_SAMPLE_RATE"
    )
    send_default_pii: bool = Field(
        default=False, validation_alias="SENTRY_SEND_DEFAULT_PII"
    )

    @field_validator(
        "traces_sample_rate",
        "profiles_sample_rate",
        mode="before",
    )
    @classmethod
    def parse_sample_rate(cls, value: object) -> float:
        # Env values are best-effort: invalid floats disable sampling
        # instead of failing service startup.
        try:
            parsed = float(str(value).strip()) if value is not None else 0.0
        except ValueError:
            return 0.0
        return max(0.0, min(1.0, parsed))

    @field_validator("send_default_pii", mode="before")
    @classmethod
    def parse_bool(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"true", "1", "yes", "on"}

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
    scrape_runtime_min_free_bytes: int = Field(
        default=256 * 1024 * 1024,
        validation_alias="SCRAPE_RUNTIME_MIN_FREE_BYTES",
    )
    runtime_root: Path = Field(default=Path("/tmp/scrape"))
    environment: str = Field(default="production", validation_alias="ENVIRONMENT")
    sentry: SentrySettings = Field(default_factory=SentrySettings)

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

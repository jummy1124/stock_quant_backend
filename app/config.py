from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = "postgresql+psycopg://user:pass@localhost:5432/userdata"
    JWT_SECRET: str = "change-me"
    JWT_EXPIRE_MINUTES: int = 1440
    JWT_ALGORITHM: str = "HS256"
    ALLOWED_ORIGINS: str = "*"
    APP_PORT: int = 8100
    # Shared secret the screener (stock_market run_intraday) presents in the
    # X-Ingest-Token header when POSTing daily screening snapshots. Empty value
    # disables the ingest endpoint (returns 503) so it can't be hit unconfigured.
    INGEST_TOKEN: str = ""

    @property
    def allowed_origins_list(self) -> list[str]:
        value = self.ALLOWED_ORIGINS.strip()
        if value == "*":
            return ["*"]
        return [o.strip() for o in value.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

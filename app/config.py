from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Minimum acceptable length for the HS256 signing key. RFC 7518 §3.2 requires a
# key at least as long as the hash output (32 bytes for SHA-256); PyJWT warns
# below that. 32 characters of a urlsafe token is comfortably above it.
_MIN_JWT_SECRET_LEN = 32

_JWT_SECRET_HELP = (
    "JWT_SECRET must be set to a random string of at least "
    f"{_MIN_JWT_SECRET_LEN} characters. Generate one with:\n"
    "    python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
    "and put it in .env (or the container environment). It is deliberately "
    "NOT given a default: a fallback value would let the service boot with a "
    "publicly known signing key, which lets anyone forge a token for any user."
)


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = "postgresql+psycopg://user:pass@localhost:5432/userdata"

    # --- JWT ---
    # No default: the app must fail fast rather than silently sign tokens with
    # a well-known secret. See _JWT_SECRET_HELP above.
    JWT_SECRET: str
    JWT_EXPIRE_MINUTES: int = 1440
    JWT_ALGORITHM: str = "HS256"

    ALLOWED_ORIGINS: str = "http://localhost:5173"
    APP_PORT: int = 8100

    # Shared secret the screener (stock_market run_intraday) presents in the
    # X-Ingest-Token header when POSTing daily screening snapshots. Empty value
    # disables the ingest endpoint (returns 503) so it can't be hit unconfigured.
    INGEST_TOKEN: str = ""

    # --- Email links ---
    # Public base URL of the FRONTEND, used to build the links inside emails.
    # The links look like {APP_BASE_URL}/?verify=<token> and /?reset=<token>.
    APP_BASE_URL: str = "http://localhost:5173"

    # --- Email delivery ---
    # "console" -> render the message to the application log (dev default, no
    #              SMTP account needed, the link is right there in the log).
    # "smtp"    -> actually send via SMTP (see the SMTP_* settings below).
    EMAIL_BACKEND: str = "console"
    EMAIL_FROM: str = "no-reply@localhost"
    EMAIL_FROM_NAME: str = "Stock Quant"

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_STARTTLS: bool = True  # port 587 (submission). Set false when using SSL.
    SMTP_SSL: bool = False  # port 465 (implicit TLS). Mutually exclusive with STARTTLS.
    SMTP_TIMEOUT: float = 10.0

    # --- Token lifetimes ---
    EMAIL_VERIFY_TTL_MINUTES: int = 1440  # 24h — clicked at leisure
    PASSWORD_RESET_TTL_MINUTES: int = 30  # short: it can change the password

    # --- Rate limiting ---
    RATE_LIMIT_ENABLED: bool = True

    @field_validator("JWT_SECRET")
    @classmethod
    def _check_jwt_secret(cls, v: str) -> str:
        v = v.strip()
        if len(v) < _MIN_JWT_SECRET_LEN:
            raise ValueError(_JWT_SECRET_HELP)
        if v in {"change-me", "changeme", "secret", "please-change-me"}:
            raise ValueError(_JWT_SECRET_HELP)
        return v

    @field_validator("EMAIL_BACKEND")
    @classmethod
    def _check_email_backend(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"console", "smtp"}:
            raise ValueError("EMAIL_BACKEND must be 'console' or 'smtp'.")
        return v

    @property
    def allowed_origins_list(self) -> list[str]:
        value = self.ALLOWED_ORIGINS.strip()
        if value == "*":
            return ["*"]
        return [o.strip() for o in value.split(",") if o.strip()]

    @property
    def app_base_url(self) -> str:
        """APP_BASE_URL without a trailing slash, for safe link concatenation."""
        return self.APP_BASE_URL.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

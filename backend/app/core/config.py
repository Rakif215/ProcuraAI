"""
app/core/config.py
------------------
All environment-variable config for Orion, loaded via Pydantic BaseSettings.
Access anywhere with:  from app.core.config import settings
"""
from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────────
    app_name: str = "Orion"
    app_env: str = "development"
    secret_key: str = "change-me"
    frontend_url: str = "http://localhost:5173"
    backend_public_url: str = "http://localhost:8000"
    cors_origins: str = ""

    # ── Supabase ─────────────────────────────────────────────────────────────
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    database_url: str  # asyncpg connection string

    # ── AI Providers ─────────────────────────────────────────────────────────
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct:free"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "deepseek-ai/deepseek-v4-flash"

    # ── Pilot usage/budget protection ────────────────────────────────────────
    ai_daily_calls_per_tenant: int = 250
    ai_daily_calls_per_user: int = 80
    ai_daily_tokens_per_tenant: int = 250_000
    ai_daily_tokens_per_user: int = 80_000
    ai_triage_hard_limit_per_sync: int = 10

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Security ──────────────────────────────────────────────────────────────
    encryption_key: str = ""  # Fernet key for encrypting stored credentials

    # ── Email defaults ────────────────────────────────────────────────────────
    default_imap_host: str = "mail.spacemail.com"
    default_imap_port: int = 993
    default_smtp_host: str = "mail.spacemail.com"
    default_smtp_port: int = 465

    # ── Google OAuth2 (Gmail) ─────────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def allowed_origins(self) -> list[str]:
        configured = [
            origin.strip()
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]
        defaults = [
            self.frontend_url,
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
            "http://localhost:8081",
            "http://127.0.0.1:8081",
        ]
        return list(dict.fromkeys(configured + defaults))

    @property
    def google_oauth_redirect_uri(self) -> str:
        if self.google_redirect_uri:
            return self.google_redirect_uri
        return f"{self.backend_public_url.rstrip('/')}/api/v1/onboarding/google/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

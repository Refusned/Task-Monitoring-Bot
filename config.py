"""Application configuration loaded from .env / environment."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single source of configuration for the bot."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Behavior and money safety
    dry_run: bool = True
    # Money-safety knobs cannot go negative.
    auto_accept_threshold_cost: float = Field(default=0.0, ge=0)  # 0 = always require human
    daily_spend_limit: float = Field(default=1000.0, ge=0)
    per_order_spend_limit: float = Field(default=200.0, ge=0)
    auto_reject_uncertain_results: bool = True
    db_path: Path = Path("./exchange_monitor.db")
    order_poll_interval_seconds: int = Field(default=300, ge=10)
    posts_poll_interval_seconds: int = Field(default=900, ge=30)
    metrica_verification_window_days: int = Field(default=7, ge=1)

    # Telegram
    telegram_bot_token: str = ""
    telegram_admin_ids: list[int] = []

    # Exchange credentials
    smmcode_api_key: str = ""
    prskill_api_key: str = ""
    unu_api_key: str = ""
    advego_api_token: str = ""
    ipgold_api_key: str = ""

    # Yandex Metrica. Empty values => TrafficVerifier in mock mode.
    metrica_counter_id: str = ""
    metrica_oauth_token: str = ""
    youtube_data_api_key: str = ""

    # Google Sheets
    google_sheets_credentials_file: str = ""
    google_sheets_spreadsheet_id: str = ""

    # Targets (always config, never hardcoded)
    target_website_url: str = ""
    target_social_accounts: list[str] = []

    # LLM autopilot (Ollama-compatible /api/chat)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    ollama_timeout_seconds: float = Field(default=30.0, ge=1)
    autopilot_candidate_limit_per_exchange: int = Field(default=8, ge=1)

    # Browser dashboard
    web_dashboard_host: str = "127.0.0.1"
    web_dashboard_port: int = Field(default=8080, ge=1, le=65535)
    web_dashboard_token: str = ""


def get_settings() -> Settings:
    """Instantiate settings; pydantic-settings reads .env and the environment."""
    return Settings()

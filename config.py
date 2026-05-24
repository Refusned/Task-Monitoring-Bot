"""Application configuration loaded from .env / environment.

All values that depend on assumptions A1-A12 (see CLAUDE.md) live here —
so a wrong assumption is a config change, not a code change.
"""

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

    # Behavior — A5, money safety
    dry_run: bool = True
    # MEDIUM-f fix from Day 1 audit: money-safety knobs cannot go negative.
    auto_accept_threshold_cost: float = Field(default=0.0, ge=0)  # 0 = always require human
    daily_spend_limit: float = Field(default=1000.0, ge=0)
    per_order_spend_limit: float = Field(default=200.0, ge=0)
    db_path: Path = Path("./exchange_monitor.db")

    # Telegram (Day 6)
    telegram_bot_token: str = ""
    telegram_admin_ids: list[int] = []

    # Exchanges — populated from test_task.txt at deploy time
    smmcode_api_key: str = ""
    prskill_api_key: str = ""
    unu_api_key: str = ""
    advego_api_token: str = ""
    ipgold_api_key: str = ""

    # Yandex Metrica — A10. Empty values => TrafficVerifier in mock mode.
    metrica_counter_id: str = ""
    metrica_oauth_token: str = ""

    # Google Sheets — A9
    google_sheets_credentials_file: str = ""
    google_sheets_spreadsheet_id: str = ""

    # Targets — A2 (always config, never hardcoded)
    target_website_url: str = ""
    target_social_accounts: list[str] = []


def get_settings() -> Settings:
    """Instantiate settings; pydantic-settings reads .env and the environment."""
    return Settings()

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
    advego_default_campaign_id: int | None = None  # required for advego.create_order
    ipgold_api_key: str = ""

    # Yandex Metrica — A10. Empty values => no verifier wired.
    metrica_counter_id: str = ""  # numeric counter id from metrika.yandex.ru/list/
    metrica_oauth_token: str = ""  # get via `python scripts/yandex_oauth.py` or /api/oauth/yandex/*
    metrica_verification_window_days: int = Field(default=7, ge=1, le=90)

    # Yandex OAuth — for bootstrapping metrica_oauth_token (one-time flow).
    yandex_oauth_client_id: str = ""
    yandex_oauth_client_secret: str = ""

    # Telegram MTProto verifier (Telethon, user-level API).
    # api_id + api_hash: register at https://my.telegram.org/apps
    # session_string: produced by scripts/telegram_mtproto_setup.py (one-time)
    telegram_mtproto_api_id: int = 0
    telegram_mtproto_api_hash: str = ""
    telegram_mtproto_session_string: str = ""

    # Google Sheets — A9
    google_sheets_credentials_file: str = ""
    google_sheets_spreadsheet_id: str = ""

    # Targets — A2 (always config, never hardcoded)
    target_website_url: str = ""
    target_social_accounts: list[str] = []

    # ===== Agent-layer settings (v4 pivot) =====

    # Ollama Cloud — the LLM behind OpenClaw.
    ollama_base_url: str = "https://ollama.com/api"
    ollama_api_key: str = ""
    ollama_model: str = "kimi-k2.6"
    ollama_fallback_model: str = "qwen3:32b"

    # FastAPI app (tools + dashboard). Dashboard and agent tools intentionally
    # use different secrets: the LLM must never receive the browser/admin token.
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    dashboard_token: str = ""  # generated on first run if empty; printed to console
    agent_tools_token: str = ""  # generated at runtime if empty; only for /api/tools/*
    # Used to build deep-links in Telegram notifications. Falls back to
    # public_hostname:app_port when empty.
    dashboard_base_url: str = ""
    public_hostname: str = ""  # e.g. "2.26.110.148" when the server binds 0.0.0.0

    # Verifiers — only YouTube is real in MVP.
    youtube_api_key: str = ""

    # Cache TTL for exchange_balance_cache (seconds).
    balance_cache_ttl_seconds: int = 60

    # Verification polling
    verifier_poll_interval_seconds: int = 60
    verifier_timeout_minutes: int = 60

    # Anti-fraud re-verification window. After an order is auto-completed, the
    # scheduler re-checks its metric at T+min...T+max to detect SMM panels that
    # deliver counters that decay (likes "stick" then drop within 24-72h).
    recheck_min_age_seconds: int = 3600     # 1h after completion = first checkpoint
    recheck_max_age_seconds: int = 259200   # 72h after completion = final freeze
    recheck_interval_seconds: int = 3600    # re-check job cadence
    recheck_fraud_drop_threshold: float = Field(default=0.30, ge=0.0, le=1.0)
    # ↑ 0.30 = if measured value dropped >30% vs previous reading, flag as fraud.

    # Mock prices in RUB per unit. Realistic ranges for RU SMM panels (2026).
    # Format: {exchange}:{platform}:{metric}=price_per_unit;eta_min;eta_max
    # — semicolons inside an entry, commas between.
    mock_quotes_csv: str = (
        "smmcode:youtube:likes=0.45;5;30,"
        "smmcode:youtube:views=0.10;30;120,"
        "smmcode:youtube:subscribes=2.0;60;240,"
        "smmcode:youtube:comments=3.0;30;180,"
        "prskill:youtube:likes=0.55;5;30,"
        "prskill:youtube:views=0.12;30;120,"
        "prskill:youtube:subscribes=2.5;60;240,"
        "unu:youtube:views=0.11;30;180,"
        "unu:youtube:likes=0.60;10;60,"
        "advego:youtube:comments=2.5;30;180,"
        "advego:youtube:likes=0.72;10;60,"
        "ipgold:youtube:views=0.09;60;240,"
        "ipgold:youtube:likes=0.65;10;60"
    )

    # Auto-resume placing an order when balance becomes available after a topup.
    auto_resume_after_topup: bool = True

    # OpenClaw runtime path (optional — `python cli.py start` will only print
    # SOUL.md path and instructions if this is empty).
    openclaw_binary: str = ""
    openclaw_profile: str = "smm-agent"
    openclaw_workspace: str = "./openclaw-ws"
    openclaw_agent_timeout_seconds: int = 120
    openclaw_gateway_http_url: str = "http://127.0.0.1:19010/"
    openclaw_agent_retries: int = Field(default=2, ge=0, le=5)
    openclaw_preflight_enabled: bool = True
    openclaw_restart_command: str = ""


def get_settings() -> Settings:
    """Instantiate settings; pydantic-settings reads .env and the environment."""
    return Settings()

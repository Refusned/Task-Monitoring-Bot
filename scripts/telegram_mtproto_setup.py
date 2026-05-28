"""Interactive bootstrap of TELEGRAM_MTPROTO_SESSION_STRING.

Telethon's "StringSession" packs the entire authorized session into a single
opaque string — convenient for .env. This script:

    1. Asks for api_id / api_hash if not already in .env
       (register an app at https://my.telegram.org/apps).
    2. Starts a TelegramClient with an empty StringSession.
    3. Prompts for phone number → sends code → reads code.
    4. (If 2FA enabled) prompts for password.
    5. Prints the resulting StringSession — paste into .env as
       TELEGRAM_MTPROTO_SESSION_STRING=<value>.

Run:
    python scripts/telegram_mtproto_setup.py

The session string is sensitive — it grants full access to this Telegram
account. Keep .env at mode 600.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings


async def _main() -> int:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print("error: telethon not installed; run `pip install telethon`")
        return 2

    s = get_settings()
    api_id = s.telegram_mtproto_api_id or _ask_int("api_id (from my.telegram.org/apps): ")
    api_hash = s.telegram_mtproto_api_hash or _ask_str("api_hash: ")
    if not api_id or not api_hash:
        print("error: api_id and api_hash are required")
        return 2

    print()
    print("Starting Telethon login flow…")
    print("You'll be asked for: phone number → SMS code → (optional) 2FA password")
    print()

    client = TelegramClient(StringSession(), int(api_id), str(api_hash))
    try:
        # `.start()` runs the full interactive auth flow on stdin/stdout.
        await client.start()
        me = await client.get_me()
        session_string = client.session.save()
        print()
        print(f"Logged in as: {me.first_name or ''} {me.last_name or ''} (@{me.username or me.id})")
        print()
        print("Paste these lines into .env and chmod 600 the file:")
        print()
        print(f"TELEGRAM_MTPROTO_API_ID={api_id}")
        print(f"TELEGRAM_MTPROTO_API_HASH={api_hash}")
        print(f"TELEGRAM_MTPROTO_SESSION_STRING={session_string}")
        print()
        return 0
    finally:
        await client.disconnect()


def _ask_str(prompt: str) -> str:
    return input(prompt).strip()


def _ask_int(prompt: str) -> int:
    raw = input(prompt).strip()
    try:
        return int(raw)
    except ValueError:
        print(f"error: expected integer, got {raw!r}")
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(_main()))

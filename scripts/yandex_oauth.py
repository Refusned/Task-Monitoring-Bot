"""Interactive bootstrap of METRICA_OAUTH_TOKEN.

Yandex' "OOB" OAuth flow (redirect_uri=oauth.yandex.ru/verification_code) hands
the user a one-time `code` on the browser page. This script:

    1. Prints the authorize URL.
    2. Reads the `code` from stdin.
    3. POSTs to https://oauth.yandex.ru/token to exchange `code` → token.
    4. Prints the token (and refresh_token + expiry) so you can paste into .env.

Run:
    python scripts/yandex_oauth.py

Env (reads from .env):
    YANDEX_OAUTH_CLIENT_ID
    YANDEX_OAUTH_CLIENT_SECRET

Required Metrica scopes are baked into the registered app — no `scope` parameter
needed; Yandex returns whatever the app was configured with at registration.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as `python scripts/yandex_oauth.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from config import get_settings

_AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
_TOKEN_URL = "https://oauth.yandex.ru/token"


def authorize_url(client_id: str) -> str:
    return f"{_AUTHORIZE_URL}?response_type=code&client_id={client_id}"


def exchange_code_sync(client_id: str, client_secret: str, code: str) -> dict:
    """Exchange a one-time `code` for an OAuth token. Returns the full payload
    (`access_token`, `refresh_token`, `expires_in`, ...). Raises on HTTP error."""
    response = httpx.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code.strip(),
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()


async def exchange_code(
    client: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
    code: str,
) -> dict:
    """Async variant used by the FastAPI endpoint."""
    response = await client.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code.strip(),
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()


def _main() -> int:
    s = get_settings()
    if not s.yandex_oauth_client_id or not s.yandex_oauth_client_secret:
        print("error: YANDEX_OAUTH_CLIENT_ID and YANDEX_OAUTH_CLIENT_SECRET must be in .env")
        return 2
    print()
    print("1. Open this URL in a browser, authorize, copy the code Yandex shows:")
    print()
    print(f"   {authorize_url(s.yandex_oauth_client_id)}")
    print()
    code = input("2. Paste the code here, then Enter: ").strip()
    if not code:
        print("error: empty code")
        return 2
    try:
        data = exchange_code_sync(s.yandex_oauth_client_id, s.yandex_oauth_client_secret, code)
    except httpx.HTTPStatusError as exc:
        print(f"error: Yandex returned {exc.response.status_code}: {exc.response.text}")
        return 1
    except httpx.HTTPError as exc:
        print(f"error: transport: {exc!r}")
        return 1
    token = data.get("access_token")
    if not token:
        print(f"error: no access_token in response: {data!r}")
        return 1
    print()
    print("3. Add to .env (and restart the backend):")
    print()
    print(f"   METRICA_OAUTH_TOKEN={token}")
    if data.get("refresh_token"):
        print(f"   # refresh_token={data['refresh_token']}  (for later renewal)")
    if data.get("expires_in"):
        print(f"   # expires_in={data['expires_in']}s")
    print()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())

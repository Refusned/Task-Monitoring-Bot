#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

read_setting() {
  .venv/bin/python - "$1" <<'PY'
from config import get_settings
import sys

print(getattr(get_settings(), sys.argv[1]))
PY
}

OPENCLAW_BINARY="$(read_setting openclaw_binary)"
if [[ -z "$OPENCLAW_BINARY" ]]; then
  OPENCLAW_BINARY="openclaw"
fi

export OLLAMA_API_KEY="$(read_setting ollama_api_key)"
export AGENT_TOOLS_TOKEN="$(read_setting agent_tools_token)"
export APP_BASE_URL="http://127.0.0.1:$(read_setting app_port)"

exec "$OPENCLAW_BINARY" gateway run --force

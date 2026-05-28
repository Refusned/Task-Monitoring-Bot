#!/usr/bin/env bash
set -Eeuo pipefail

cd /root/smm-agent

load_dotenv() {
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *"="* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < .env
}

if [[ -f .env ]]; then
  load_dotenv
fi

export HOME=/root
export APP_BASE_URL="${APP_BASE_URL:-http://127.0.0.1:${APP_PORT:-8765}}"

cd /root/smm-agent/openclaw-ws
exec "${OPENCLAW_BINARY:-openclaw}" \
  --profile "${OPENCLAW_PROFILE:-smm-agent}" \
  gateway run --force

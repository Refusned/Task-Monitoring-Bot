#!/usr/bin/env python3
"""Self-healing health check for the SMM agent deployment.

Systemd restarts dead processes. This watchdog also restarts services that are
alive but not answering their local health endpoints.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BACKEND_SERVICE = "smm-agent-backend.service"
OPENCLAW_SERVICE = "smm-agent-openclaw.service"
BACKEND_HEALTH_URL = "http://127.0.0.1:8765/healthz"
OPENCLAW_HEALTH_URL = "http://127.0.0.1:19010/"
LOG_PATH = Path("/root/smm-agent/logs/watchdog.jsonl")
STATE_PATH = Path("/root/smm-agent/logs/watchdog_state.json")
HTTP_FAILURE_THRESHOLDS = {
    BACKEND_SERVICE: 3,
    OPENCLAW_SERVICE: 2,
}


def log(event: str, **payload: object) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "event": event,
        **payload,
    }
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def service_state(service: str) -> tuple[str, str]:
    result = run("systemctl", "show", service, "-p", "ActiveState", "-p", "SubState")
    active = "unknown"
    sub = "unknown"
    for line in result.stdout.splitlines():
        if line.startswith("ActiveState="):
            active = line.split("=", 1)[1]
        elif line.startswith("SubState="):
            sub = line.split("=", 1)[1]
    return active, sub


def service_active(service: str) -> bool:
    active, sub = service_state(service)
    return active == "active" and sub == "running"


def load_state() -> dict[str, int]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, int]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status >= 500:
                return False
            if url.endswith("/healthz"):
                body = response.read(1024 * 64)
                data = json.loads(body.decode("utf-8"))
                return bool(data.get("backend") and data.get("db"))
            return True
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return False


def restart(service: str, reason: str) -> None:
    result = run("systemctl", "restart", service)
    log(
        "restart",
        service=service,
        reason=reason,
        returncode=result.returncode,
        stdout=result.stdout[-1000:],
        stderr=result.stderr[-1000:],
    )


def check_service(service: str, url: str, state: dict[str, int]) -> None:
    active, sub = service_state(service)
    if active in {"inactive", "failed"}:
        restart(service, "inactive")
        state[service] = 0
        time.sleep(3)
        return
    if active != "active" or sub != "running":
        log("transition", service=service, active=active, substate=sub)
        return

    if not http_ok(url):
        failures = state.get(service, 0) + 1
        state[service] = failures
        threshold = HTTP_FAILURE_THRESHOLDS.get(service, 3)
        if failures >= threshold:
            restart(service, f"healthcheck_failed_{failures}x")
            state[service] = 0
        else:
            log(
                "healthcheck_warn",
                service=service,
                failures=failures,
                threshold=threshold,
            )
        return

    state[service] = 0


def main() -> int:
    state = load_state()
    check_service(OPENCLAW_SERVICE, OPENCLAW_HEALTH_URL, state)
    check_service(BACKEND_SERVICE, BACKEND_HEALTH_URL, state)
    save_state(state)
    log(
        "ok",
        backend_active=service_active(BACKEND_SERVICE),
        openclaw_active=service_active(OPENCLAW_SERVICE),
        backend_http=http_ok(BACKEND_HEALTH_URL),
        openclaw_http=http_ok(OPENCLAW_HEALTH_URL),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

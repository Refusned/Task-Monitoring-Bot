# Exchange Order Monitor Bot — v4 pivot: LLM-агент-оркестратор + дашборд

После пересмотра ТЗ заказчиком превращён из «монитор заказов» в **полностью
автономного LLM-агента** на OpenClaw + Ollama Cloud. Пользователь пишет в
Telegram цель на естественном языке («накрути 200 лайков на YT-видео»), агент:

1. Параллельно запрашивает котировки у всех 5 бирж через `get_quote`.
2. Получает балансы по каждой бирже (`get_balances`).
3. Выбирает первую (от дешёвой к дорогой) биржу, где денег хватает.
4. Снимает baseline-метрики на платформе (`capture_snapshot`) и размещает заказ.
5. Если денег ни на одной бирже не хватает — отдаёт пользователю ссылку на
   пополнение **самой дешёвой** биржи (`get_topup_info`); scheduler следит за
   её балансом и автоматически возобновляет заказ после пополнения.
6. После завершения заказа scheduler сам сверяет дельту с baseline через
   `PlatformVerifier` (только YouTube Data API в MVP) и отчитывается пользователю
   в Telegram + пуш на дашборд.

«Единой кассы» у бота нет — деньги живут на стороне бирж, бот не proxy для
платежей. Полное обсуждение и обоснование — в плане `~/.claude/plans/…`.

## Стек

- Python 3.11+ · asyncio
- **OpenClaw** + `SOUL.md` (агент-шелл + Telegram-канал)
- **Ollama Cloud** + `kimi-k2.6` (LLM, tool-calling); fallback `qwen3:32b`
- **FastAPI** (tool-endpoints для агента + дашборд + WebSocket)
- HTMX + TailwindCSS + DaisyUI + Chart.js (через CDN, без npm-сборки)
- APScheduler (poll + verify + topup-recheck)
- aiogram 3 (legacy CLI-демки) · aiosqlite WAL · pydantic v2 · httpx
- pytest + ruff

## Архитектура

```
User (Telegram) ──► OpenClaw + SOUL.md ──► Ollama Cloud (kimi-k2.6)
                                                │ HTTP tool calls
                                                ▼
                    FastAPI (single process)
                    ├─ /api/tools/* (8 endpoints для OpenClaw)
                    ├─ /dashboard + /api/state/* (HTMX + WS)
                    ├─► Orchestrator + 5 adapters (existing C1/C2)
                    ├─► PlatformVerifier (YouTube Data API)
                    ├─► APScheduler (poll → verify → recheck balances)
                    └─► SQLite WAL
```

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env             # реквизиты 5 бирж
cp agent/.env.example agent/.env # ключи Ollama / Telegram / dashboard token
```

## Запуск

```bash
# Sanity-check: схема БД, регистрация адаптеров/верификаторов, мок-котировки.
python cli.py agent-smoke

# Backend: FastAPI tools + dashboard + scheduler в одном процессе.
# Токен дашборда генерируется в консоль при первом старте.
DRY_RUN=false python cli.py start

# OpenClaw в отдельном процессе (см. https://docs.openclaw.ai):
#   openclaw run agent/SOUL.md
# (агент сам подключится к http://127.0.0.1:8000/api/tools/* с Bearer-токеном)

# Старые команды Day-1..3 (legacy) — всё ещё работают для контрольных проверок:
python cli.py smoke
python cli.py demo
python cli.py create-order --exchange smmcode --scenario activity_like --target https://... --quantity 100 --max-cost 0.50 --service-id <id>
```

Дашборд: `http://127.0.0.1:8000/dashboard` —
балансы по биржам, активные заказы, pending topups, live-лента tool-calls
агента (главный showcase), graph расходов.

## Тесты и линт

```bash
pytest
ruff check
```

## Безопасность

- Все секреты — в `.env` (не коммитится).
- `DASHBOARD_TOKEN` генерируется автоматически при первом старте; вставляется в форму входа.
- `AGENT_TOOLS_TOKEN` отделён от дашборда и передаётся только OpenClaw tools runtime.
- Идемпотентность денежных операций (C1 — не разместить дважды, C2 — не оплатить
  дважды) реализована на уровне Python+SQLite, **не** на правилах SOUL.md.
- Реальные creds 5 бирж — в `../test_task.txt` (вне репозитория).

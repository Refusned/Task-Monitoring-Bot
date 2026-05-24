# Exchange Order Monitor Bot

Бот, управляющий жизненным циклом заказов на 5 биржах накрутки и микрозадач:
**создаёт заказы → мониторит → проверяет результат → оплачивает (принимает) или
возвращает на доработку.**

Тестовое задание на вакансию ИИ-инженера. Полный контекст и принятые допущения по
неполному ТЗ — в [CLAUDE.md](./CLAUDE.md). Архитектура и решения — в
[docs/DESIGN.md](./docs/DESIGN.md).

## Стек

Python 3.11+, asyncio · aiogram 3 (Telegram) · httpx (HTTP, XML-RPC для advego) ·
APScheduler · aiosqlite в режиме WAL · pydantic v2 · gspread · pytest + ruff.

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # реальные значения подставляются перед работой с биржами
```

## Запуск

```bash
# Day 1 — smoke-harness: проверяет, что config, БД и адаптеры рабочие
python cli.py smoke

# Day 3+ — полный DRY_RUN цикл: создание + мониторинг + верификация
python cli.py demo

# Day 2 — создать заказ на выбранной бирже (в DRY_RUN — через fake-адаптер)
python cli.py create-order \
  --exchange fake_panel \
  --scenario activity_subscribe \
  --target https://t.me/example_channel \
  --quantity 10 \
  --max-cost 2.0 \
  --dry-run

# Day 3+ — мониторинг и верификация вручную
python cli.py monitor --dry-run
python cli.py verify --order-uuid <uuid>
```

## Тесты и линт

```bash
pytest
ruff check
```

## Статус

Все 5 бирж адаптированы, оркестратор, верификация, Telegram-бот, отчётность и e2e тесты реализованы. 118 тестов проходят, линтер чистый. MVP готов к запуску (DRY_RUN) и интеграции реальных API-ключей.

## Безопасность

Все секреты — в `.env` (не коммитится). Реальные креды бирж лежат в `../test_task.txt`
(вне репозитория). Подробнее — [CLAUDE.md → Безопасность](./CLAUDE.md#безопасность--важно).

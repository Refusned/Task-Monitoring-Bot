# AGENTS.md — Task Monitoring Bot

Контекст проекта для Codex и других coding agents.

## Продукт

Task Monitoring Bot — коммерческий Telegram-бот для управления заказами на
SMM-панелях и биржах микрозадач. Он создаёт заказы, мониторит выполнение,
проверяет результат независимо от статуса биржи, помогает принять работу или
вернуть её на доработку и формирует недельную отчётность.

Публичный репозиторий не содержит клиентские ключи, рабочие URL и приватные
операционные данные.

## Что важно сохранить

- Денежные действия должны быть идемпотентными.
- `DRY_RUN=true` — безопасный дефолт.
- Секреты никогда не коммитятся, не логируются и не показываются в Telegram.
- SQLite — source of truth; Google Sheets — только выгрузка.
- Изменяемые клиентские параметры должны оставаться config-driven.
- Не рефакторить соседний код без необходимости.

## Архитектура

```text
Telegram bot + CLI
       |
       v
Orchestrator
       |
       +--> adapters/
       +--> verification/
       +--> db/
       +--> reporting/

APScheduler:
poll_active_orders / poll_new_posts / weekly_report
```

## Стек

- Python 3.11+
- asyncio
- aiogram 3
- httpx
- APScheduler
- aiosqlite WAL
- pydantic + pydantic-settings
- gspread
- pytest
- ruff

## Биржи

| Биржа | Тип | Комментарий |
|---|---|---|
| smmcode.shop | SMM-панель | каталог, баланс, создание заказов |
| prskill.ru | SMM-панель | Perfect Panel style API |
| unu.im | биржа микрозадач | задачи, тарифы, отчёты |
| advego.com | биржа микрозадач | XML-RPC |
| ipgold.ru | биржа микрозадач | capability-gated адаптер |

`PanelAdapter` и `TaskExchangeAdapter` разделены намеренно: у SMM-панелей нет
per-submission accept/rework, у бирж микрозадач есть.

## Инварианты

### C1: не создать заказ дважды

- Сначала локальная строка `orders(status='creating')`.
- Затем внешний API-вызов.
- После успеха `external_order_id` и `active`.
- Старые `creating`-строки обрабатываются reconcile при старте.

### C2: не оплатить дважды

- `payments(exchange, external_submission_id)` уникален.
- Claim сабмишена делается условным update.
- Внешний HTTP-вызов идёт вне DB-транзакции.
- Результат пишется через `action_log`.

## Сценарии

- `activity_subscribe`
- `activity_like`
- `social_traffic`

Для трафика источники: VK, X, YouTube, Telegram, Dzen, Pinterest.

## Проверка

- Трафик: `TrafficVerifier`, Яндекс.Метрика + UTM.
- Активность: `ActivityVerifier`, baseline + финальный счётчик.
- Вердикты: `auto_pass`, `needs_human_review`, `fail`.

## Команды

```bash
python cli.py smoke
python cli.py monitor --dry-run
python cli.py verify ...
python cli.py create-order ...
python main.py
pytest
ruff check .
ruff format --check .
```

## Конвенции

- Патчи должны быть хирургическими.
- Сначала читать существующий код и тесты.
- Для поиска использовать `rg`.
- Не трогать `.env`, БД, логи, runtime state.
- Добавлять тесты пропорционально риску изменения.
- При правке пользовательских текстов избегать рекламного шума: лучше конкретные
  факты, архитектура и проверяемые результаты.

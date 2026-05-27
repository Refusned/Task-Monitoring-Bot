# CLAUDE.md — Task Monitoring Bot

Технический контекст проекта для AI-assisted разработки.

## Что это

Task Monitoring Bot — коммерческий Telegram-бот с LLM-автопилотом, который ведёт
lifecycle заказов на SMM-панелях и биржах микрозадач:

`цель пользователя -> Ollama -> выбор услуги по цене -> создать -> проверить -> отчитаться`

Публичный репозиторий не содержит клиентские секреты, рабочие цели и приватные
детали. Всё окружение задаётся через `.env`.

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

## Основные модули

```text
adapters/        интеграции с биржами
autopilot/       Ollama planner, selector, goal runner
bot/             Telegram handlers и keyboards
db/              schema, WAL, claim helpers, audit log
web_dashboard/   FastAPI dashboard and browser UI
verification/   TrafficVerifier и ActivityVerifier
reporting/      Google Sheets export
posts/          watcher новых постов
tests/          unit, integration, fault-injection, live smoke
cli.py          smoke / monitor / verify / create-order / dashboard
main.py         Telegram polling + scheduler
orchestrator.py state machine, C1/C2, audit
config.py       pydantic-settings
models.py       доменные модели и статусы
```

## Биржи

| Биржа | Тип | API | Accept/rework |
|---|---|---|---|
| smmcode.shop | SMM-панель | reseller / Perfect Panel style | нет |
| prskill.ru | SMM-панель | reseller / Perfect Panel style | нет |
| unu.im | биржа микрозадач | API v1 | да |
| advego.com | биржа микрозадач | XML-RPC | да |
| ipgold.ru | биржа микрозадач | методы частично подтверждены | capability-gated |

Два типа площадок моделируются явно:

- `PanelAdapter`: заказ предоплачен, нет per-submission lifecycle.
- `TaskExchangeAdapter`: исполнители сдают отчёты, админ принимает или возвращает.

## Lifecycle

### OrderStatus

- `draft`
- `creating`
- `active`
- `verifying`
- `completed`
- `failed`
- `cancelled`

### SubmissionStatus

- `new`
- `verifying`
- `awaiting_admin`
- `accepting`
- `accepted`
- `rejecting`
- `rework_requested`
- `failed`

## Инварианты

### C1: не разместить заказ дважды

- `orders` получает статус `creating` до внешнего API-вызова.
- После успеха пишется `external_order_id`, статус становится `active`.
- Старые `creating`-строки обрабатываются при старте через reconcile.

### C2: не оплатить сабмишен дважды

- `payments(exchange, external_submission_id)` уникален.
- Перед внешним accept вызывается atomic claim сабмишена.
- HTTP-вызов идёт вне DB-транзакции.
- Результат фиксируется через `action_log`.

## Сценарии

| Сценарий | Модель |
|---|---|
| Подписки | `Scenario.ACTIVITY_SUBSCRIBE` |
| Лайки | `Scenario.ACTIVITY_LIKE` |
| Просмотры | `Scenario.ACTIVITY_VIEW` |
| Трафик из соцсетей | `Scenario.SOCIAL_TRAFFIC` |

## LLM Autopilot

- Ollama endpoint: `POST /api/chat`.
- Config: `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_TIMEOUT_SECONDS`.
- Structured output model: `AutopilotIntent`.
- LLM only parses the user's goal. Exchange/service selection is deterministic:
  adapters return `ServiceOption`, `AutopilotRunner` picks the cheapest candidate
  that fits quantity and max_cost.
- In `DRY_RUN=true`, autopilot returns a plan and does not call exchange create APIs.
- In live mode, activity goals that need public counters capture baseline before
  order creation. YouTube subscribers/likes/views require `YOUTUBE_DATA_API_KEY`;
  without a measurable baseline the runner refuses the paid order.

Источник трафика задаётся через `SourcePlatform`: VK, X, YouTube, Telegram, Dzen,
Pinterest.

## Проверка результата

- `TrafficVerifier`: Яндекс.Метрика + UTM, mock/live режим.
- `ActivityVerifier`: baseline + финальный счётчик активности; YouTube
  subscribers/likes/views read public counters through YouTube Data API.
- Вердикты: `auto_pass`, `needs_human_review`, `fail`.

## Конфигурация

Все runtime-параметры через `.env`:

- `DRY_RUN`
- лимиты трат;
- Telegram token и admin ids;
- API-ключи бирж;
- Яндекс.Метрика;
- `YOUTUBE_DATA_API_KEY`;
- Google Sheets;
- целевой сайт и соцсети.

`.env`, БД, логи и state-файлы не коммитятся.

## Команды

```bash
python cli.py smoke
python cli.py autopilot --goal "500 лайков на https://youtube.com/watch?v=..." --plan-only
python cli.py dashboard
python cli.py monitor --dry-run
python cli.py verify ...
python cli.py create-order ...
python main.py
pytest
ruff check .
ruff format --check .
```

## Статус

Реализованы:

- адаптеры 5 площадок;
- orchestrator полного lifecycle;
- Telegram bot;
- CLI;
- verification layer;
- post watcher;
- Google Sheets reporter;
- SQLite audit и money-safety;
- тесты и ruff.

Публичный запуск должен оставаться в `DRY_RUN=true`, пока не подключены приватные
ключи и не подтверждены лимиты.

# DESIGN.md — Task Monitoring Bot

Архитектурный разбор коммерческого Telegram-бота с LLM-автопилотом для управления
заказами на SMM-панелях и биржах микрозадач.

## Контекст

Клиенту нужен был инструмент, где оператор пополняет баланс и ставит цель обычным
текстом: ссылка + лайки, просмотры, подписчики или переходы. Дальше бот должен сам
понять задачу, выбрать площадку по цене, создать заказ, проверить факт выполнения и
показать итоговые метрики. Важное ограничение: бот работает с реальными деньгами на
внешних площадках, поэтому LLM не получает права напрямую вызывать биржи. Она только
структурирует цель, а исполнение остаётся детерминированным.

Публичный репозиторий не содержит ключей, клиентских целей и приватных данных. Все
такие значения задаются через `.env`.

## Цели продукта

| Цель | Реализация |
|---|---|
| Создавать заказы на нескольких площадках | `PanelAdapter` и `TaskExchangeAdapter` для 5 бирж |
| Принимать цель обычным текстом | Ollama `/api/chat` -> `AutopilotIntent` |
| Выбирать лучшую цену автоматически | `AutopilotRunner` сравнивает `ServiceOption.price_per_unit` |
| Вести уже созданные заказы | Импорт по `external_order_id` и общий lifecycle |
| Проверять результат независимо | `TrafficVerifier` и `ActivityVerifier` |
| Защищать денежные действия | C1/C2-инварианты, лимиты, audit log |
| Дать удобный интерфейс оператору | Telegram FSM, reply/inline keyboards, CLI для техподдержки |
| Делать недельный отчёт | SQLite `report_rows` -> Google Sheets |

## Архитектура

```text
Telegram bot + CLI
       |
       v
LLM Autopilot
Ollama structured output -> price selector
       |
       v
Orchestrator
state machine, audit, money-safety
       |
       +--> Exchange adapters
       |    smmcode / prskill / unu / advego / ipgold
       |
       +--> Verification
       |    TrafficVerifier / ActivityVerifier
       |
       +--> SQLite WAL
            orders / submissions / payments / action_log / report_rows

APScheduler:
poll_active_orders / poll_new_posts / weekly_report
```

### Orchestrator

`orchestrator.py` держит lifecycle заказа и не знает деталей конкретной биржи.
Адаптеры сообщают capabilities, а orchestrator вызывает только доступные операции:
создание заказа, получение статуса, список сабмишенов, принятие, возврат, баланс.

Основные статусы:

- `OrderStatus`: `draft`, `creating`, `active`, `verifying`, `completed`, `failed`,
  `cancelled`.
- `SubmissionStatus`: `new`, `verifying`, `awaiting_admin`, `accepting`, `accepted`,
  `rejecting`, `rework_requested`, `failed`.

### Адаптеры

| Адаптер | Тип | Комментарий |
|---|---|---|
| `smmcode` | SMM-панель | REST/JSON, каталог услуг, баланс, создание заказов |
| `prskill` | SMM-панель | Perfect Panel style API |
| `unu` | Биржа микрозадач | Задачи, отчёты, тарифы, баланс |
| `advego` | Биржа микрозадач | XML-RPC поверх async `httpx` |
| `ipgold` | Биржа микрозадач | Capability-gated адаптер под неподтверждённые методы |

Разделение на `PanelAdapter` и `TaskExchangeAdapter` важно: SMM-панели обычно
предоплачены и не дают per-submission accept/reject, а биржи микрозадач работают с
отчётами исполнителей.

### Autopilot

`autopilot/` добавляет новый путь:

1. `OllamaPlanner` вызывает `POST /api/chat` с `stream=false` и JSON Schema в `format`.
2. Модель возвращает `AutopilotIntent`: сценарий, цель, количество, источник трафика
   и опциональный бюджет.
3. `AutopilotRunner` запрашивает каталоги всех доступных адаптеров.
4. Кандидаты без цены, вне min/max quantity или дороже бюджета отбрасываются.
5. Самый дешёвый кандидат превращается в `OrderSpec`.
6. Создание заказа идёт через тот же C1-safe путь, что и ручной flow.
7. Сомнительные проверки в live-режиме автоматически отклоняются политикой
   `AUTO_REJECT_UNCERTAIN_RESULTS=true`, чтобы оператор не становился ручным
   bottleneck.

LLM не выбирает биржу и не получает API-ключи. Это намеренное разделение: модель
понимает фразу пользователя, код отвечает за деньги.

## Money-safety

### C1: защита от двойного создания заказа

1. До внешнего API-вызова создаётся локальный заказ со статусом `creating`.
2. После успеха пишется `external_order_id`, заказ переводится в `active`.
3. Ошибка фиксируется как `failed` и пишется в audit log.
4. При старте процесса `reconcile_creating()` обрабатывает старые `creating`-строки.

Это закрывает crash между локальным состоянием и внешней биржей.

### C2: защита от двойной оплаты

1. `payments(exchange, external_submission_id)` имеет уникальный ключ.
2. Claim сабмишена делается conditional update в БД.
3. Внешний HTTP-вызов выполняется после commit, без длинной транзакции.
4. Результат фиксируется через `action_log`.
5. Повторное нажатие inline-кнопки не создаёт второе денежное действие.

Если процесс упал после записи payment-row, но до перевода сабмишена в терминальный
статус, следующий проход восстанавливает состояние без повторного API-вызова.

## Verification

Бот не принимает статус биржи как единственный источник истины.

| Проверка | Источник данных | Вердикт |
|---|---|---|
| Трафик на сайт | Яндекс.Метрика + UTM-фильтры | `auto_pass`, `needs_human_review`, `fail` |
| Подписки / лайки / просмотры | Baseline + финальный счётчик активности | `auto_pass`, `needs_human_review`, `fail` |

Пороговые значения, окна проверки и mock/live режимы задаются конфигом.

## Telegram и CLI

Telegram — основной интерфейс оператора. CLI оставлен намеренно: он нужен для smoke,
support-сценариев, локальной диагностики и автоматизированных проверок.

Ключевые команды:

```bash
python cli.py smoke
python cli.py autopilot --goal "500 лайков на https://youtube.com/watch?v=..." --plan-only
python cli.py monitor --dry-run
python cli.py verify ...
python cli.py create-order ...
python main.py
```

## Конфигурация

Проект config-driven. Целевые аккаунты, сайт, лимиты, API-ключи, режимы проверки,
Google Sheets и Telegram-админы задаются через `.env`.

Секреты не коммитятся:

- `.env`
- runtime DB
- логи
- lock/pid/state файлы

В репозитории остаётся только `.env.example`.

## Trade-offs

| Решение | Почему так |
|---|---|
| Ollama structured output вместо свободного текста | План можно валидировать Pydantic-моделью до любых денежных действий |
| LLM только парсит цель, selector выбирает биржу | Цена, лимиты и service_id должны быть проверяемыми, а не доверенными модели |
| SQLite WAL вместо отдельного Postgres | Достаточно для одного production-инстанса, проще деплой и бэкап |
| `DRY_RUN=true` по умолчанию | Публичный запуск не должен создавать реальные заказы |
| Capability flags вместо жёстких веток по биржам | Новую площадку проще добавить без правки orchestrator |
| HTTP-вызов вне DB-транзакции | Внешний API не должен держать локальные locks |
| Mock mode для Метрики и бирж | Демо и тесты не зависят от приватных credentials |
| Auto-reject uncertain results | Для полностью автоматического режима сомнительный результат безопаснее вернуть, чем оплачивать |

## Качество

Проверки проекта:

```bash
pytest
ruff check .
ruff format --check .
```

Покрытые зоны:

- контрактные тесты адаптеров;
- Ollama autopilot parsing and cheapest-service selection;
- lifecycle заказов и сабмишенов;
- C1/C2 money-safety;
- fault injection;
- Telegram FSM и admin gating;
- read-only live smoke при наличии credentials;
- token sanitization.

## Ограничения

- Публичный репозиторий не содержит клиентские ключи и реальные рабочие цели.
- `DRY_RUN=true` должен оставаться дефолтом для локального запуска.
- Auto-accept денежных действий включается только осознанно через конфиг.
- Проект работает через доступные API и не закладывает обход антибот-защит.

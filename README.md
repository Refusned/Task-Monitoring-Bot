<div align="center">

# Task Monitoring Bot

**Коммерческий Telegram-бот с LLM-автопилотом для заказов на SMM-панелях и биржах микрозадач.**

`цель пользователя -> Ollama -> выбор лучшей цены -> создание заказа -> проверка -> отчёт`

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)](https://docs.aiogram.dev/)
[![Tests](https://img.shields.io/badge/tests-232%20passing-brightgreen)](#-качество)
[![Lint](https://img.shields.io/badge/ruff-clean-success)](#-качество)
[![Deploy](https://img.shields.io/badge/deploy-systemd-blue)](#-деплой)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

</div>

---

## О проекте

Task Monitoring Bot автоматизирует рутинную работу заказчика с 5 площадками:
`smmcode.shop`, `prskill.ru`, `unu.im`, `advego.com`, `ipgold.ru`.

До этого процесс был ручным: зайти на биржу, создать заказ, дождаться статуса,
проверить результат в сторонних системах, принять работу или вернуть исполнителю,
а затем собрать недельный отчёт. Бот забирает этот цикл в Telegram и CLI:

1. Принимает цель обычным текстом: ссылка + лайки, просмотры, подписчики или трафик.
2. Разбирает цель через Ollama API в строгий JSON-план.
3. Сравнивает каталоги бирж и выбирает самый дешёвый подходящий service_id.
4. Создаёт заказ через существующий money-safe lifecycle.
5. Мониторит статус, проверяет факт выполнения и собирает отчёты.

Проект делался как клиентская разработка: с реальными API, реальными ограничениями
по деньгам и приватными доступами, которые не попадают в репозиторий. Имя клиента,
ключи и рабочие цели скрыты; публичная версия оставляет архитектуру, тесты и
демо-режим.

---

## Что показывает этот проект

Это не учебный CRUD и не обёртка над одним API. Внутри есть задачи, которые хорошо
показывают уровень backend/automation-разработки:

- **5 внешних интеграций** с разными протоколами: REST/JSON, form API, XML-RPC поверх
  `httpx`, read-only live smoke для проверенных ключей.
- **LLM-autopilot через Ollama**: свободный текст превращается в валидируемый
  `AutopilotIntent`, а деньги тратит только детерминированный код после проверки каталогов.
- **Денежный lifecycle**: заказ нельзя случайно разместить дважды, сабмишен нельзя
  оплатить повторно.
- **Асинхронная оркестрация**: Telegram bot + scheduler + CLI работают поверх одного
  состояния в SQLite WAL.
- **Browser dashboard**: оператор видит сводку, заказы, проверки, отчёты, балансы
  и может запускать LLM-автопилот из браузера.
- **Независимая верификация**: бот не доверяет словам биржи, а сверяет результат через
  Яндекс.Метрику, UTM и счётчики активности.
- **Удобный Telegram UX**: сценарии на кнопках, динамический каталог услуг, ручной
  fallback, админские подтверждения.
- **Тестовая сетка**: контрактные тесты адаптеров, fault injection, Telegram FSM,
  money-safety кейсы, live read-only проверки.

---

## Сценарии

| Сценарий | Что делает бот | Как проверяет |
|---|---|---|
| Подписки в соцсетях | Создаёт заказ на подписку на целевой аккаунт | Сравнивает baseline и итоговый счётчик |
| Лайки на новые посты | Находит новые посты и готовит заказ на лайки | Сравнивает рост активности по посту |
| Просмотры | Заказывает просмотры на пост или YouTube-видео | Сравнивает baseline и итоговый счётчик |
| Переходы из соцсетей | Заказывает трафик из ВК, X, YouTube, Telegram, Дзен, Pinterest | Считает визиты в Яндекс.Метрике по UTM |
| Ручной импорт | Подхватывает заказ, который уже создан на бирже | Ведёт его тем же lifecycle |

---

## Архитектура

```text
 Telegram bot (aiogram 3)        CLI             Browser dashboard
 /goal /new_order /dashboard     autopilot       FastAPI /api/*
          \                       |                /
           \                      |               /
            v                     v              v
        +--------------------------------+
        |      LLM Autopilot             |
        | Ollama -> intent -> selector   |
        +--------------------------------+
                    |
                    v
        +--------------------------------+
        |         Orchestrator           |
        | state machine + audit + C1/C2  |
        +--------------------------------+
          |             |              |
          v             v              v
   Exchange adapters  Verification   SQLite WAL
   smmcode           TrafficVerifier orders
   prskill           ActivityVerifier submissions
   unu                               payments
   advego                            action_log
   ipgold                            report_rows
          ^
          |
   APScheduler
   poll_active_orders / poll_new_posts / weekly_report
          |
          v
   Google Sheets export
```

Адаптеры разделены по типу площадки:

| Тип | Площадки | Особенность |
|---|---|---|
| `PanelAdapter` | smmcode, prskill | Заказ предоплачен, нет per-submission accept/reject |
| `TaskExchangeAdapter` | unu, advego, ipgold | Исполнители сдают отчёты, админ принимает или возвращает |

Каждый адаптер декларирует capability-флаги: `CREATE_ORDER`, `LIST_SUBMISSIONS`,
`ACCEPT_SUBMISSION`, `REJECT_SUBMISSION`, `GET_BALANCE`. Оркестратор смотрит на
capabilities и не делает хрупких `isinstance`-ветвлений по конкретным биржам.

---

## Money-safety

В проекте есть два инварианта, вокруг которых построен lifecycle.

### C1: заказ не размещается дважды

Перед внешним API-вызовом бот создаёт локальную строку `orders` в статусе
`creating`. После успешного ответа биржи пишет `external_order_id` и переводит заказ
в `active`. Если процесс упал посередине, startup reconcile поднимает старые
`creating`-строки и переводит их в безопасный статус вместо повторного размещения.

### C2: сабмишен не оплачивается дважды

Оплата защищена на уровне БД и приложения:

- `payments(exchange, external_submission_id)` имеет уникальный ключ;
- claim сабмишена делается атомарным conditional update;
- HTTP-вызов к бирже выполняется вне DB-транзакции;
- результат фиксируется через `action_log`;
- повторный callback в Telegram не создаёт вторую оплату.

Это важнее красивого UI: бот работает с деньгами, поэтому отказоустойчивость здесь
часть продукта, а не техническая деталь.

---

## Telegram UX

Бот спроектирован для человека, который хочет поставить цель, пополнить баланс и
получить итоговые метрики.

- Reply keyboard всегда под рукой: цель автопилоту, новый заказ, баланс, сводка,
  заказы, проверка, очередь на ручное решение, отчёт, здоровье, отмена.
- `/goal` принимает фразу вроде `500 лайков на https://youtube.com/watch?v=...`.
- LLM разбирает цель, но не выбирает биржу напрямую: выбор делает код по живым
  каталогам и цене.
- Inline FSM ведёт по шагам: сценарий -> биржа -> услуга -> URL -> количество ->
  подтверждение. Это остаётся ручным fallback.
- Каталог услуг подтягивается с биржи и фильтруется под сценарий.
- Перед созданием заказа показывается итоговая карточка с ценой и лимитами.
- Денежные действия доступны только администратору.
- CLI остаётся для smoke-проверок, отладки и автоматизации.

---

## Интеграции

| Интеграция | Реализация |
|---|---|
| Ollama | `POST /api/chat`, `stream=false`, JSON Schema в `format` |
| Telegram | `aiogram 3`, FSM, reply/inline keyboards, admin whitelist |
| smmcode.shop | REST/JSON + Perfect Panel style form API |
| prskill.ru | REST/JSON + каталог услуг |
| unu.im | API v1, задачи, отчёты, балансы, тарифы |
| advego.com | XML-RPC поверх async `httpx` |
| ipgold.ru | capability-gated адаптер под неподтверждённые методы API |
| Яндекс.Метрика | проверка трафика по UTM |
| Google Sheets | еженедельная выгрузка во вкладку «Трафик из соц сетей» |

---

## Качество

```bash
pytest
ruff check .
ruff format --check .
```

Покрытие проверок:

| Блок | Что проверяется |
|---|---|
| Autopilot | Ollama structured output, выбор самой дешёвой услуги, dry-run/live ветки |
| Адаптеры | Парсинг JSON/XML, статусы, ошибки API, каталоги услуг |
| Orchestrator | Lifecycle заказов, C1/C2, audit log, reconcile |
| Telegram bot | FSM, callbacks, admin gating, full lifecycle через synthetic updates |
| Verification | TrafficVerifier, ActivityVerifier, mock/live режимы |
| Fault injection | Network errors, malformed payloads, mid-call crashes, token sanitization |
| Live smoke | Read-only вызовы к реальным API при наличии credentials |

Текущая публичная сборка: **232 tests passing**, `ruff` clean.

---

## Быстрый старт

```bash
git clone https://github.com/Refusned/Task-Monitoring-Bot.git
cd Task-Monitoring-Bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

По умолчанию включён безопасный режим:

```env
DRY_RUN=true
```

Локальная проверка без денежных действий:

```bash
python cli.py smoke
python cli.py autopilot --goal "500 лайков на https://youtube.com/watch?v=..." --plan-only
python cli.py dashboard
python cli.py monitor --dry-run
pytest
```

Запуск Telegram-бота:

```bash
python main.py
```

---

## Конфигурация

Все изменяемые параметры вынесены в `.env`.

| Переменная | Назначение |
|---|---|
| `DRY_RUN` | Блокирует write-действия на биржах |
| `DAILY_SPEND_LIMIT`, `PER_ORDER_SPEND_LIMIT` | Суточный лимит и лимит на заказ |
| `AUTO_REJECT_UNCERTAIN_RESULTS` | В live-режиме возвращает сомнительные результаты без ручного решения |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_IDS` | Telegram bot и список админов |
| `SMMCODE_API_KEY`, `UNU_API_KEY`, `ADVEGO_API_TOKEN`, `PRSKILL_API_KEY`, `IPGOLD_API_KEY` | Доступы к биржам |
| `METRICA_COUNTER_ID`, `METRICA_OAUTH_TOKEN` | Проверка трафика через Яндекс.Метрику |
| `YOUTUBE_DATA_API_KEY` | Baseline и финальная проверка подписчиков/лайков/просмотров YouTube |
| `GOOGLE_SHEETS_CREDENTIALS_FILE`, `GOOGLE_SHEETS_SPREADSHEET_ID` | Еженедельный отчёт |
| `TARGET_WEBSITE_URL`, `TARGET_SOCIAL_ACCOUNTS` | Рабочие цели клиента |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_TIMEOUT_SECONDS` | LLM-автопилот |
| `WEB_DASHBOARD_HOST`, `WEB_DASHBOARD_PORT`, `WEB_DASHBOARD_TOKEN` | Browser dashboard |

`.env` и runtime-файлы находятся в `.gitignore`. В репозитории есть только
`.env.example` без секретов.

Для Яндекс.Метрики в `.env` нужен `METRICA_COUNTER_ID` и готовый
`METRICA_OAUTH_TOKEN`. OAuth Client ID/secret используются только для получения
токена через Yandex OAuth и не должны попадать в репозиторий.

Для live-заказов на подписчиков, лайки и просмотры YouTube автопилот сначала снимает baseline
через YouTube Data API. Если счётчик недоступен, заказ не создаётся: так бот не
запускает платную задачу, результат которой потом нельзя доказать.

Browser dashboard запускается командой `python cli.py dashboard`. По умолчанию он
слушает только `127.0.0.1:8080`; при биндинге наружу нужно задать
`WEB_DASHBOARD_TOKEN`, иначе команда откажется стартовать.

---

## Деплой

Бот рассчитан на запуск как обычный Linux service: отдельный пользователь, venv,
file lock против второго процесса, restart policy, закрытые права на `.env`.

Пример unit-файла:

```ini
[Unit]
Description=Exchange Order Monitor Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=exchangebot
Group=exchangebot
WorkingDirectory=/opt/exchange-monitor-bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/exchange-monitor-bot/.venv/bin/python /opt/exchange-monitor-bot/main.py
Restart=always
RestartSec=10
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

---

## Структура

```text
.
├── adapters/          # 5 бирж, base ABC, capability flags
├── autopilot/         # Ollama planner, selector, goal runner
├── bot/               # aiogram handlers и keyboards
├── db/                # schema, WAL setup, claim helpers, audit log
├── web_dashboard/     # FastAPI dashboard + browser UI
├── verification/      # TrafficVerifier и ActivityVerifier
├── reporting/         # Google Sheets writer
├── posts/             # watcher новых постов
├── tests/             # unit, integration, fault-injection, live smoke
├── docs/
│   ├── DESIGN.md      # архитектурный разбор
│   └── api/           # заметки по API бирж
├── cli.py             # smoke / monitor / verify / create-order / dashboard
├── main.py            # Telegram polling + scheduler
├── orchestrator.py    # state machine и money-safety
├── config.py          # pydantic-settings
└── models.py          # доменные модели и статусы
```

---

## Документация

- [docs/DESIGN.md](./docs/DESIGN.md) — архитектура, lifecycle, инварианты, trade-offs.
- [docs/api](./docs/api/) — заметки по API каждой площадки.
- [docs/demo.html](./docs/demo.html) — локальная презентация продукта.
- [CLAUDE.md](./CLAUDE.md) — технический контекст для AI-assisted разработки.

---

## Безопасность и комплаенс

- Только авторизованные аккаунты и предоставленные клиентом доступы.
- Без обхода антибот-систем и без сбора чужих учётных данных.
- Денежные действия подтверждаются администратором, auto-accept включается только
  через конфиг.
- Все write-действия блокируются `DRY_RUN=true` в публичной конфигурации.
- Токены маскируются в логах и Telegram-сообщениях.

---

## Лицензия

MIT — см. [LICENSE](LICENSE).

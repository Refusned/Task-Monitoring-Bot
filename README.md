<div align="center">

# 🤖 Task Monitoring Bot

**Production-ready Telegram-бот, ведущий жизненный цикл заказов на 5 биржах накрутки и микрозадач.**

`создаёт заказы → мониторит → независимо проверяет результат → оплачивает или возвращает на доработку`

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)](https://docs.aiogram.dev/)
[![Tests](https://img.shields.io/badge/tests-187%20passing-brightgreen)](#-тесты)
[![Lint](https://img.shields.io/badge/ruff-clean-success)](#-качество-кода)
[![Deployed](https://img.shields.io/badge/deployment-systemd%2024%2F7-blue)](#-деплой)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

</div>

---

## 📖 О проекте

Бот автоматизирует операционку при работе с биржами **накрутки активности в соцсетях** (подписки, лайки) и **закупки целевого трафика на сайт**. Заказчик описывает заказ в Telegram через интерактивный мастер, бот размещает его на одной из 5 бирж, ведёт мониторинг, независимо проверяет результат (через Яндекс.Метрику или счётчики активности — **не доверяя статусу биржи**) и предлагает оплатить или вернуть на доработку.

Покрытие — **5 бирж**: `smmcode.shop`, `prskill.ru`, `unu.im`, `advego.com`, `ipgold.ru`. Разные API-протоколы: REST/JSON, Perfect Panel form, XML-RPC.

> 🟢 **Текущий статус.** Бот развёрнут как systemd-сервис на выделенном Linux-сервере, работает 24/7, отвечает в Telegram как [@monitoring_tasks_bot](https://t.me/monitoring_tasks_bot). Подтверждённые балансы и каталоги (smmcode: 2934 услуги, unu: 56 тарифов, advego: 27 типов заказов). Размещение реальных заказов разблокируется одним переключателем `DRY_RUN=false`.

---

## ✨ Что внутри

### Полный жизненный цикл заказа
| Этап | Что делает бот |
|---|---|
| **Создание** | Интерактивный мастер: сценарий → биржа → услуга из каталога → URL → количество → подтверждение. Estimate-first cost-cap до отправки на биржу. |
| **Мониторинг** | Опрос статуса биржи по расписанию + per-exchange изоляция сбоёв. |
| **Верификация** | Независимая проверка через Яндекс.Метрику (UTM-фильтр) или дельту счётчиков активности. **Не доверяет** статусу биржи (инвариант A4). |
| **Оплата / возврат** | Auto-pass / auto-reject по порогам или вынос на ручное решение админа в Telegram. Деньги всегда под защитой C2-инварианта. |
| **Отчётность** | Еженедельный отчёт в Google Sheets (вкладка «Трафик из соц сетей»). |

### Интерактивный Telegram UX
- **Reply keyboard** под полем ввода всегда видна: новый заказ, сводка, заказы, проверка, на проверку, отчёт, здоровье, отмена.
- **Inline-кнопки** на каждом шаге FSM. Ничего печатать не нужно — кроме URL и количества.
- **Динамический каталог** услуг: после выбора биржи бот тянет её актуальный каталог, фильтрует по сценарию (подписки / лайки / трафик), показывает топ-N с ценами как кнопки.
- Manual entry fallback для случаев, когда нужного нет в каталоге или биржа недоступна.
- **Slash-команды как алиасы** для power-users: 10 команд с эмодзи и русскими описаниями в `getMyCommands`.

<details>
<summary>Скрин Telegram (опционально, после первого реального запроса)</summary>

```
📦 Новый заказ

Сценарий: activity_subscribe
Биржа: smmcode

Шаг 3/6 — выберите услугу:

  [Подписчики РФ · ₽0.05/шт · ≥50]
  [Подписчики со всего мира · ₽0.18/шт · ≥20]
  [✏️ Ввести ID вручную]
  [❌ Отмена]
```
</details>

---

## 🏗 Архитектура

```
 Telegram bot (aiogram 3)        CLI (cli.py)
    ▼                              ▼
 Reply keyboard + inline FSM     create-order / monitor / verify / smoke
        └───────────┬─────────────┘
                    ▼
        ┌──────────────────────────┐
        │     Orchestrator         │   State-machine + C1/C2 + audit
        │  (ACTIVE → VERIFYING →   │
        │   COMPLETED / FAILED)    │
        └──────────────────────────┘
            │           │           │
   ┌────────┘           │           └─────────┐
   ▼                    ▼                     ▼
 Adapters         Verification          DB (aiosqlite WAL)
 ─ smmcode        ─ TrafficVerifier      orders · submissions
 ─ prskill        ─ ActivityVerifier     payments(PK) · action_log
 ─ unu                                   verifications · audit_log
 ─ advego                                report_rows · watcher_state
 ─ ipgold (stub)
                                             ▲
                                             │
        ┌────────────────────────────────────┘
        │
 APScheduler (max_instances=1, coalesce=True)
   ─ poll_active_orders   (полминуты — конфиг)
   ─ poll_new_posts       (A3: PostWatcher для «лайки на новые посты»)
   ─ weekly_report        (понедельник 09:00 UTC → Google Sheets)
```

**Per-operation capability flags** делают orchestrator robust к разнородным API: каждый адаптер декларирует `CREATE_ORDER`, `LIST_SUBMISSIONS`, `ACCEPT_SUBMISSION` и т.д. Если функции нет — orchestrator корректно её не вызывает.

**Два типа бирж** моделируются явно:
- `PanelAdapter` (smmcode, prskill) — заказ предоплачен, нет accept/reject цикла.
- `TaskExchangeAdapter` (unu, advego, ipgold) — per-submission accept/reject лайфцикл с реальными исполнителями.

---

## 💰 Money-safety: C1 + C2 инварианты

Самая сложная часть — гарантировать что **на бирже не появится дубль заказа** и **сабмишен не будет оплачен дважды**. Документировано в `docs/DESIGN.md` + кодом enforce'нуто на 3 уровнях (БД-схема, helper'ы, оркестратор).

### C1 — никаких дублей размещения

| Шаг | Что происходит |
|---|---|
| 1 | `INSERT INTO orders (status='creating', ...)` **до** вызова API биржи |
| 2 | Вызов `adapter.create_order(spec, client_order_uuid)` |
| 3 | На успехе — `UPDATE ... SET status='active', external_order_id=?` (conditional, raises если rowcount ≠ 1) |
| 4 | На ошибке — `UPDATE ... SET status='failed'` + audit `order_create_failed` |
| 5 | При старте процесса — `reconcile_creating(min_age_seconds=300)` переводит orphan-CREATING строки старше 5 мин в FAILED |

**Result:** даже при crash'е процесса посреди создания — orphan-строка не висит вечно. Защита от двойного размещения на бирже работает.

### C2 — никаких двойных платежей

Три уровня защиты:

1. **БД-уровень:** `payments (exchange, external_submission_id) PRIMARY KEY` + partial unique index `uq_submissions_order_external` на `submissions(order_uuid, external_submission_id) WHERE external_submission_id IS NOT NULL`. Два конкурентных поллера физически не могут вставить дубль.

2. **Атомарный claim:** `claim_submission_and_open_action()` за один commit делает condition UPDATE статуса (`NEW|VERIFYING|AWAITING_ADMIN → ACCEPTING`) **и** открывает `action_log` row. Никакого окна между «застолбили действие» и «записали что застолбили».

3. **HTTP-вызов вне транзакции:** паттерн «занять → commit → внешний вызов → записать результат». DB-транзакции никогда не висят на время внешнего HTTP.

### Heal-on-already-paid

Если предыдущая попытка крашнулась **после** записи в `payments`, но **до** перевода `submissions` в терминальный статус — следующий вызов обнаружит payment-row и **залечит** статус (action_log → succeeded, submission → ACCEPTED), без повторного API-вызова.

### Estimate-first cost cap

Перед вызовом `/create_order` адаптер тянет каталог биржи (кэш), считает `cost = price × quantity` и **отказывает** если превышен `spec.max_cost`. UNU специально проверен — раньше `price=0` обходил cap, теперь fallback `min_price_rub → price → cost` с обязательным positive check.

---

## 🧪 Тесты

```
187 passed, 9 skipped (live API; пропущены без credentials)
```

| Категория | Количество | Что покрывают |
|---|---|---|
| Адаптеры (контрактные) | 100+ | mock HTTP responses, парсинг JSON/XML, статус-маппинг, error paths |
| Audit-fixes | 9 | CRITICAL-1/2, HIGH-3/4/5 от Codex review (race-safe persistence, atomic claim, age-gated reconcile) |
| Bot interactive UX | 23 | FSM, reply-keyboard handlers, callbacks, admin gating |
| Bot full lifecycle | 4 | end-to-end через synthetic Update'ы |
| Fault injection | 13 | network errors, malformed JSON/XML, mid-call crashes, token leakage, isolation |
| Live API (read-only) | 9 | реальные вызовы к smmcode/unu/advego — balance, services, catalogue |
| Live cost-cap (real API, $0) | 4 | подтверждение что estimate-first отказывает до `/create_order` против live-каталогов |
| Service step (новый) | 13 | catalogue filter, ServiceOption, manual fallback, keyboard layout |
| Day 1 audit | 16 | оригинальные регрессионные тесты |

Запуск:
```bash
pytest                       # все 187 тестов
pytest tests/test_e2e_real.py        # реальные API (требуется .env с ключами)
pytest tests/test_live_roundtrip.py  # money-safety против live API ($0 trade)
ruff check . && ruff format --check .
```

---

## 🚀 Quick start

### Установка
```bash
git clone https://github.com/Refusned/Task-Monitoring-Bot.git
cd Task-Monitoring-Bot
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # → заполнить credentials, оставить DRY_RUN=true для проверки
```

### Локальный smoke (без денег)
```bash
python cli.py smoke                  # проверка config + БД + балансов бирж
python cli.py monitor --dry-run      # read-only обзор активных заказов
pytest                               # 187 тестов
```

### Запуск бота
```bash
python main.py
# Telegram-сторона: открыть @monitoring_tasks_bot, нажать /start
```

### Production deployment (systemd)
См. `scripts/launchctl/` для macOS, или [systemd unit на сервере](#-деплой).

---

## ⚙️ Конфигурация

Всё через `.env` (config-driven; неверная интерпретация ТЗ = правка `.env`, а не кода):

| Переменная | Назначение |
|---|---|
| `DRY_RUN` | `true` = блокирует размещение реальных заказов (safety) |
| `DAILY_SPEND_LIMIT`, `PER_ORDER_SPEND_LIMIT` | Жёсткие лимиты трат |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_IDS` | Авторизация в Telegram |
| `SMMCODE_API_KEY`, `UNU_API_KEY`, `ADVEGO_API_TOKEN`, `PRSKILL_API_KEY`, `IPGOLD_API_KEY` | Ключи бирж |
| `METRICA_COUNTER_ID`, `METRICA_OAUTH_TOKEN` | Яндекс.Метрика (mock-режим если пусто) |
| `GOOGLE_SHEETS_CREDENTIALS_FILE`, `GOOGLE_SHEETS_SPREADSHEET_ID` | Еженедельный отчёт |
| `TARGET_WEBSITE_URL`, `TARGET_SOCIAL_ACCOUNTS` | Цели для PostWatcher и отчётности |

---

## 📡 Деплой

Текущее боевое развёртывание на Ubuntu 24.04:

```ini
# /etc/systemd/system/exchange-monitor-bot.service
[Unit]
Description=Exchange Order Monitor Bot - Telegram + scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=exchangebot                    # неprivileged user
Group=exchangebot
WorkingDirectory=/opt/exchange-monitor-bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/exchange-monitor-bot/.venv/bin/python /opt/exchange-monitor-bot/main.py
Restart=always
RestartSec=10
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
StandardOutput=append:/opt/exchange-monitor-bot/logs/bot.log
StandardError=append:/opt/exchange-monitor-bot/logs/bot.log

[Install]
WantedBy=multi-user.target
```

— автоматический рестарт при крэше, изолированный неpriv-пользователь, `NoNewPrivileges=true`, UMask 0077, единый file-lock против двойного запуска.

---

## 📂 Структура

```
.
├── adapters/             # 5 бирж + base ABC + ServiceOption DTO + capability flags
│   ├── base.py           # ExchangeAdapter, PanelAdapter, TaskExchangeAdapter
│   ├── smmcode.py        # REST/JSON, Perfect Panel form
│   ├── prskill.py        # REST/JSON, Perfect Panel form
│   ├── unu.py            # REST/JSON, native v1 protocol
│   ├── advego.py         # XML-RPC поверх httpx (async-native)
│   └── ipgold.py         # capability-gated stub (API не подтверждён)
├── bot/                  # aiogram 3
│   ├── handlers.py       # FSM + slash commands + reply keyboard
│   └── keyboards.py      # Inline + reply keyboards
├── db/
│   ├── database.py       # aiosqlite, WAL, claim helpers, audit log
│   └── schema.sql        # 7 таблиц + partial unique index + CHECK constraints
├── orchestrator.py       # state machine, C1/C2 enforcement, scheduling
├── verification/         # TrafficVerifier (Метрика+UTM), ActivityVerifier
├── reporting/sheets.py   # gspread, weekly report
├── posts/watcher.py      # A3: детектор новых постов в Telegram-каналах
├── scheduler.py          # APScheduler integration
├── cli.py                # smoke / monitor / verify / create-order
├── main.py               # Telegram polling + APScheduler entry point
├── config.py             # pydantic-settings, всё из .env
├── models.py             # OrderStatus, SubmissionStatus, OrderSpec, ...
├── tests/                # 187 тестов
├── docs/
│   ├── DESIGN.md         # архитектура, решения, инварианты
│   └── api/              # доки по каждой бирже (smmcode, unu, advego, prskill, ipgold)
└── scripts/launchctl/    # macOS launchd plist для локальной автозагрузки
```

---

## 📐 Дизайн-решения

Подробности — в [`docs/DESIGN.md`](./docs/DESIGN.md). Кратко:

| # | Решение | Обоснование |
|---|---|---|
| **A1** | Бот не только мониторит, но и **создаёт** заказы | ТЗ прямо требует «Создание заказов» в обоих сценариях MVP |
| **A2** | Targets (URL/аккаунты) — config-driven, не hardcode | Расширяемость; смена клиентов без правки кода |
| **A3** | `PostWatcher` отслеживает новые посты автоматически | «Лайки на новые посты» в ТЗ → бот должен сам видеть новые |
| **A4** | Независимая верификация результата | Смысл бота — перепроверить перед оплатой; доверие бирже его обнуляет |
| **A5** | Оплата подтверждается админом по умолчанию | Senior-default для денежных действий; auto-accept — опт-ин по порогу |
| **A6** | Возврат на доработку — автоматический | Возврат не тратит деньги и обратим |
| **A7** | Импорт существующих заказов по `external_order_id` | ТЗ «уже РАЗМЕЩЁННЫЕ» прямым текстом |
| **A8** | 6 source-платформ из ТЗ → enum `SourcePlatform` | ВК/X/YouTube/Telegram/Дзен/Pinterest |
| **A9** | Структура отчёта Google Sheets: Неделя · Платформа · Биржа · Заказано · Факт · Стоимость · Статус | Логично выводится из ТЗ; адаптируется к существующей таблице по заголовкам |
| **A10** | Яндекс.Метрика — опционально (mock-режим без credentials) | Не блокировать разработку и демо отсутствием доступов |
| **A11** | Создание заказов и в Telegram, и в CLI | «Бот» в ТЗ → создание в Telegram; CLI нужен для тестируемости |
| **A12** | Деливерабл = рабочий MVP + `DESIGN.md` | Сильнейшая подача для теста на инженерную позицию |

---

## 🔒 Безопасность

- **Секреты только в `.env`** (gitignored). `.env.example` — шаблон с пустыми значениями.
- **DRY_RUN=true по умолчанию** блокирует создание заказов на боевых биржах. Снимается осознанно одним переключателем.
- **Подтверждение админом** перед каждой денежной операцией (configurable порог для auto-accept).
- **Лимиты трат**: per-order + daily, проверяются в той же транзакции что и вставка заказа.
- **Audit log** для каждого денежного действия + reconcile при старте.
- **Без обхода анти-бот систем** — работа только через документированные API.
- **API-токены не утекают в логи и сообщения**: error-payload sanitization (`safe_payload` без `api_token`).

---

## 🛠 Стек

| Слой | Технология |
|---|---|
| Язык | Python 3.11+ (async/await) |
| Telegram | `aiogram 3` |
| HTTP | `httpx` (включая XML-RPC поверх httpx для advego) |
| Scheduler | `APScheduler` (max_instances=1, coalesce=True) |
| БД | `aiosqlite` в режиме WAL — single source of truth |
| Config | `pydantic` + `pydantic-settings` |
| Google Sheets | `gspread` |
| Тесты | `pytest` + `pytest-asyncio` + `httpx.MockTransport` |
| Линт | `ruff check` + `ruff format` |
| Deploy | `systemd` + venv + dedicated unprivileged user |

---

## 📊 Качество кода

- ✅ `ruff check` — 0 errors
- ✅ `ruff format --check` — clean
- ✅ 187 / 187 unit + integration tests passing
- ✅ 9 / 9 live API smoke (real exchanges, $0)
- ✅ 4 / 4 live cost-cap (real exchanges, $0)
- ✅ Type hints везде (`from __future__ import annotations`)
- ✅ Docstrings на классах + publicc helpers
- ✅ Per-операционные capability-флаги вместо `isinstance` deep в логике

---

## 📚 Дополнительная документация

- [`docs/DESIGN.md`](./docs/DESIGN.md) — полный архитектурный документ, обоснования допущений A1–A12, money-safety инварианты
- [`docs/api/*.md`](./docs/api/) — доки по каждой бирже: endpoints, статус-маппинг, нюансы
- [`CLAUDE.md`](./CLAUDE.md) — заметки разработчика (внутренний контекст)

---

## 📝 Лицензия

MIT — см. [LICENSE](LICENSE).

---

<div align="center">

Разработано как тестовое задание для позиции ИИ-инженера.<br>
Production-ready бот, который **реально работает** на боевом сервере 24/7.

</div>

# prskill.ru - Reseller API (reference)

**Base URL:** `https://prskill.ru/api`
**Format:** POST, form-encoded body. Responses JSON.
**Auth:** body params `key` (in `.env` as `PRSKILL_API_KEY`) + `action`.

## Methods

| Method | Required params | Response payload |
|---|---|---|
| Список сервисов | `key`, `action=services` | `[{service, name, category, rate, min, max, fields[]}]` |
| Добавление заказа | `key`, `action=add`, `service`, `quantity`, `service_url` | `{"order": int}` |
| Проверка статуса | `key`, `action=status`, `order` | `{"orders": {"<order_id>": "process"}}` |
| Массовая проверка | `key`, `action=status`, `order` (CSV, max 50) | `{"orders": {"<id>": "<status>", ...}}` |

`rate` from `/services` is per single execution. **Total cost** = `rate * quantity`.
The bot enforces `cost <= spec.max_cost` BEFORE placing the order (estimate-first via cached `/services` lookup).

## Status string mapping (raw → normalized)

| Raw | Normalized |
|---|---|
| `process` | `in_progress` |
| `success` | `completed` |
| `cancel` | `failed` |
| `piece` | `completed` (raw status retained for review) |
| `fail` | `failed` |
| `check` | `in_progress` |

## Notes
- Max order completion time: up to 7 days (most complete on day 1).
- No balance endpoint is documented on the public API page.
- Reseller API does NOT support client-side order IDs - `client_order_uuid` (C1) lives only on our side.
- Service descriptor may contain `fields[]` with `name` and `required` booleans; `service_url` is the typical required field.

# smmcode.shop - Reseller API (reference)

**Base URL:** `https://smmcode.shop/api/reseller`
**Format:** POST, form-encoded body. Responses JSON.
**Auth:** body param `api_token` (in `.env` as `SMMCODE_API_KEY`).
**Envelope:** every successful response is `{..., "status": 200}`.

## Methods

| Method | Path | Required params | Response payload |
|---|---|---|---|
| Список услуг | `POST /services` | `api_token` | `{"services": {<platform>: {<action>: {<id>: {service_id, price, min, max, name, ...}}}}, "status": 200}` |
| Создание заказа | `POST /create_order` | `api_token`, `service_id`, `count`, `link` | `{"order_id": int, "status": 200}` |
| Статус заказа | `POST /order_status` | `api_token`, `order_id` | `{"order": {id, service_id, link, count, price, status_id, status_name}, "status": 200}` |
| Статусы заказов | `POST /orders_statuses` | `api_token`, `order_ids` (CSV, max 1000) | `{"orders": {<id>: {...}}, "status": 200}` |
| Баланс | `POST /balance` | `api_token` | `{"balance": float, "status": 200}` |
| Список статусов | `POST /statuses` | `api_token` | `{"order_statuses": {<id>: <name>}, "status": 200}` |

`price` from `/services` is per single execution. **Total cost** = `price * count`.
The bot enforces `cost <= spec.max_cost` BEFORE placing the order (estimate-first via cached `/services` lookup).

## Status code mapping (raw → normalized)

| `status_id` | `status_name` (RU) | Normalized |
|---|---|---|
| 1 | В обработке | `in_progress` |
| 2 | Не оплачено | `failed` |
| 3 | Выполнено | `completed` |
| 4 | Частично выполнено | `completed` (raw status retained for review) |
| 5 | Отменено | `failed` |
| 6 | Ошибка | `failed` |
| 7 | Выполняется | `in_progress` |
| 8 | Возврат платежа | `failed` |
| 9 | Неизвестно | `in_progress` |
| 10 | В очереди | `in_progress` |

## Notes
- Max order completion time: up to 7 days (most complete on day 1).
- Reseller validates links client-side; bad links slow execution.
- Reseller API does NOT support client-side order IDs - `client_order_uuid` (C1) lives only on our side; identity on smmcode = the returned `order_id`.

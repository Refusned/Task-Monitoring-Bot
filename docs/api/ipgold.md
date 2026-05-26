# ipgold.ru — Advertiser API v1 (reference)

**Source of truth:** official OpenAPI 1.1.7 spec ([api_docs](https://ipgold.ru/api_docs)).

| Aspect | Value |
|---|---|
| Base URL | `https://ipgold.ru/api/v1/` (or `https://ipgold.biz/api/v1/`) |
| Method | `POST` |
| Path | `/api/v1/<action>` *or* `/api/v1/` with `action` body param |
| Body | JSON, `Content-Type: application/json` |
| Auth | body param `key` = personal API token |
| Action | body param `action` = method name (required) |
| Success | `{"status": "OK", "results": ...}` |
| Error | `{"status": "BAD", "errors": [{"message": ..., "code": ...}, ...]}` |

Errors are returned with HTTP status codes **200**, **400**, **403**, **429**
all carrying the BAD envelope. The adapter parses the body BEFORE
`raise_for_status` so the API's own message reaches the operator (e.g.
`Access denied (code 403)`, `Insufficient funds (code 42)`).

## Domain model: campaigns, not orders

IPGold's primitive is the **advertising campaign**, not a one-shot SMM order:

| Phase | Action | Money? |
|---|---|---|
| Catalogue | `get_campaign_types` | no |
| Create | `create_campaign(type_id, url, actions_per_day)` | no |
| Fund | `refill_campaign(type_id, id, actions_number)` | **YES** |
| Run | `start_campaign` / `stop_campaign` | no |
| Inspect | `get_campaign_info`, `get_campaign_info_list`, `get_campaign_stat` | no |
| Configure | `change_campaign(targeting, regularity, ...)` | no |

The bot maps each Telegram-side "order" → one IPGold campaign whose lifetime
balance equals `spec.quantity` executions. The two-step flow
(create → refill) is wrapped inside our adapter's `create_order`. If create
succeeds but refill fails, the adapter surfaces the orphan campaign id so an
admin can recover via the web cabinet.

## External order id encoding

IPGold's campaign-info methods require **both** `type_id` and `id`. Our
domain model exposes a single `external_order_id` string per order, so the
adapter encodes both as `"<type_id>:<id>"` (e.g. `"24:555111"`). The
`get_order_status` method splits it back.

## Status mapping

| IPGold tuple | Normalised |
|---|---|
| `moderation_status="reject"` | `failed` |
| `moderation_status="wait"`   | `in_progress` |
| `moderation_status="success" + status="stop"` | `failed` |
| `moderation_status="success" + status="start" + balance>0` | `in_progress` |
| `moderation_status="success" + status="start" + balance==0` | `completed` |

## Why no account balance

`get_balance` is **NOT** part of the Advertiser API. Account balance only
surfaces via `refill_campaign` (which fails with error code 42 "Insufficient
funds" if exhausted) or via the web cabinet. The adapter omits
`Capability.GET_BALANCE` from `capabilities()` so the bot's `/balance`
honestly shows "баланс не отдаётся API" (consistent with advego).

## Known error codes

| code | meaning | mitigation |
|---|---|---|
| 0 | "Something is broken" (generic wrapper) | check accompanying errors |
| 4 | `Action is not specified` | always pass `action` param |
| 20 | `Unknown campaign type` | refresh `get_campaign_types` catalogue |
| 42 | `Insufficient funds in the account balance` | top up via web cabinet |
| 57 | `The *sex* field only accepts: 0, 1, 2` | (targeting; not currently used) |
| 80 | `Could not find a campaign with this type and ID` | verify `type_id`+`id` pair |
| 403 | `Access denied` | check API is activated in the account |
| 429 | `You have exceeded the request limit` | back off |

## Notes

- API tokens (`IPGOLD_API_KEY`) are read from `.env`; never committed.
- The adapter's `_post` strips `key` from any error echo so it can't leak.
- Prices in `get_campaign_types` are returned as **strings** (e.g. `"0.00015"`).
  Estimate-first cost cap converts to float and refuses if `price * quantity > spec.max_cost`.

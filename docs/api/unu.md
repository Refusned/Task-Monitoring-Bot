# unu.im - Microtask exchange API (reference)

**Endpoint:** `https://unu.im/api`
**Format:** POST. Responses JSON: `{"success": int, "errors": text, ...payload}`.
**Auth:** body params `api_key` (in `.env` as `UNU_API_KEY`) + `action`.

UNU also exposes an SMM-panel-compatible v2 protocol; the bot uses the native v1 actions.

## Relevant actions (for our scenarios)

### Money / lookup
- `get_balance` - returns `{balance: float, blocked_money: float}`.
- `get_tariffs`, `get_countries`, `get_folders` - reference data for task creation.

### Order lifecycle (= "task" in UNU terms)
- `add_task_start` - create + set execution limit in one call. Required: `name`, `descr`, `need_for_report`, `price`, `tarif_id`, `folder_id`, `add_to_limit`. Optional: `link`, targeting, time limits. Returns `{task_id: int}`.
- `add_task` - create without limit. Pair with `task_limit_add`.
- `task_limit_add` / `task_limit_sub` (`task_id`, `add_to_limit` / `sub_to_limit`).
- `task_pause` / `task_play` / `del_task` (`task_id`).
- `get_tasks` - list tasks. Optional filters: `folder_id`, `status`, `task_id`, `offset`.

### Submissions (= "reports" in UNU terms)
- `get_reports` (`task_id` required) - list reports. Each has: `id, task_id, worker_id, price_rub, status, IP, messages[]`.
- `approve_report` (`report_id`) - **pay**. No payload.
- `reject_report` (`report_id`, `comment`, `reject_type`: 1 = to revision, 2 = refuse).

## Status code mapping

### Task status (from `get_tasks`)
| Raw | Meaning | Normalized OrderStatus |
|---|---|---|
| 1 | New, needs pay (limit=0) | `creating` (we only get here if limit add failed) |
| 2 | Limit reached (all executions done) | `completed` |
| 3 | Stopped | `cancelled` |
| 4 | Active | `active` |
| 5 | Rejected by moderator | `failed` |
| 6 | On moderation | `creating`/`active` (transient) |

### Report status (from `get_reports`)
| Raw | Meaning | Normalized SubmissionStatus |
|---|---|---|
| 1 | In work | `new` |
| 2 | On review (awaiting decision) | `awaiting_admin` |
| 3 | On revision (we sent back) | `rework_requested` |
| 6 | Paid | `accepted` |

## Notes
- For our scenarios: subscriptions / likes / site visits map to UNU task types. Pick `tarif_id` via `get_tariffs` lookup (cache on adapter init).
- `add_task_start` is preferred over separate `add_task` + `task_limit_add` (one call, atomic).
- `link` parameter is optional in UNU API but our scenarios always provide it (target URL / post).
- Adapter type: **TaskExchangeAdapter** (per-submission accept/reject cycle).

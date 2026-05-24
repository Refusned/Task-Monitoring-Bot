# advego.com - Microtask / content exchange API (reference)

**Endpoint:** `https://api.advego.com/xml`
**Format:** XML-RPC POST.
**Auth:** every method takes a `token` member (in `.env` as `ADVEGO_API_TOKEN`).

XML-RPC means request bodies are XML envelopes like `<methodCall><methodName>advego.X</methodName><params>...</params></methodCall>`. Responses are XML; we deserialize into Python structures. The adapter will use `httpx` + `xml.etree.ElementTree` (or `xmlrpc.client` if simpler) to stay async-native.

## Relevant methods (for our scenarios)

### Campaigns (= "проект" in advego)
- `advego.getMyCampaigns` (`token`) - list. Each has `campaign_id`.
- `advego.addCampaign` (`token`, `title`, optional `category`) - create new.

### Order lifecycle
- `advego.addOrder` - create. Required: `token`, `campaign_id`, `title` (≤120), `description` (≤50000), `order_type` (int), `length` (int chars), `cost` (per work) OR `cost_1000` (per 1000 chars), `jobs_total` (count). Optional: `category`, `text_type` (required when `order_type=3`), `job_do_time` (0.5-120h), `pay_amount`+`length_max` (variable volume).
- `advego.startOrder` / `advego.stopOrder` (`token`, `ID`).
- `advego.editOrder` (`token`, `ID`, any params to change).
- `advego.ordersGetState` (`token`, `orders[]`) - bulk status.

### Themes (sub-tasks within an order)
- `advego.editOrderThemes` (`token`, `id_order`, `themes[]`) - each theme: `title`, `desc`, `tags`, `status='active'`, `all_count`.
- `advego.setOrderThemesOn` - activate after editing.

### Search and applications (if `is_tender=1`)
- `advego.getTenderRequests` (`token`, `id_order`).
- `advego.acceptTenderRequest` / `advego.declineTenderRequest` (`token`, `id_request`).

### Jobs (= submissions; the money-action layer)
- `advego.getJobsCompleted` - jobs awaiting moderation. Params: `token`, optional `id_order`, `campaign_id`, `date_from`, `date_to`, `date_type` (recommend `"complete"`).
- `advego.getJobsAccepted` - paid jobs (same params).
- `advego.acceptJob` (`token`, `ID`) - **pay**.
- `advego.returnJob` (`token`, `ID`, `comment` ≥10 chars) - **send to revision**.
- `advego.declineJob` (`token`, `ID`, `comment` ≥10 chars) - refuse to pay.

### Reference
- `advego.getOrderTypes`, `advego.getOrderTextTypes`, `advego.getCategories`, `advego.getLevels`, `advego.getWhiteLists`.

## Order types (relevant subset, value of `order_type`)
- `3` - Статья, обзор, SEO-копирайтинг (requires `text_type`).
- `6` - Поиск информации, полевые задания, чеки и бонусы.
- `12` - Отзовики, каталоги компаний.
- `17` - Фото и видео.
- `18` - Лайки в соцсетях.
- `19` - Тестирование, разметка данных, картинок.

Full / current list via `advego.getOrderTypes`.

## Notes
- Adapter type: **TaskExchangeAdapter** (jobs-level accept/return/decline).
- Default job execution time: 6h. Set `job_do_time` for longer tasks.
- Min costs apply (see `https://advego.com/v2/support/rules/#p9.3.1`); cheap actions like likes have a floor at ~6 RUB.
- Default level on order creation: Любители (no `approved_lists` needed). To restrict to higher levels, set `approved_lists` to system list IDs.
- Date format in API: `YYYY-MM-DD HH:MM:SS`. Use `date_type="complete"` when fetching jobs to use last-completion date (handles revisions correctly).
- Errors documented at `https://advego.com/info/api_err`.

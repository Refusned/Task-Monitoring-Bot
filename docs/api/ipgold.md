# ipgold.ru - Microtask exchange API (reference)

**Status: API methods are NOT publicly documented.**

The ipgold.ru platform offers microtask services (website visits, clicks, social media
actions, reviews). A public reseller / task-exchange API is referenced on the site but
endpoint details and parameter schemas are not available without authentication.

## Expected pattern (hypothesised, pending confirmation)

| Aspect | Expected value |
|---|---|
| Endpoint | `https://ipgold.ru/api` or similar |
| Format | POST form-encoded, JSON responses |
| Auth | `api_key` body parameter |
| Order creation | `action=create_order` or similar |
| Status check | `action=order_status` |
| Submissions | `action=list_submissions` |
| Accept / Reject | `action=accept` / `action=reject` |

## Adapter status

- `IpgoldAdapter` is implemented as a **provisional** `TaskExchangeAdapter`.
- `get_balance` returns `0.0` with a warning.
- `create_order` raises `RuntimeError` in live mode to prevent accidental placement.
- `create_order` remains disabled until the live write API is confirmed.
- The adapter will be updated as soon as real API documentation or reverse-engineered
  endpoints are available.

## Notes
- Login credentials are in `../test_task.txt` (outside the repo).
- Do not commit API keys to the repository.

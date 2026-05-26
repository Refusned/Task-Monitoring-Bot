# ipgold.ru — Advertiser API (reference)

**Endpoint:** `https://ipgold.ru/api/v2`
**Format:** POST form-encoded, JSON responses.
**Auth:** body param `api_key`.

## Confirmed shape (probed live 2026-05-26)

| Aspect | Value |
|---|---|
| Method | `POST` |
| Endpoint | `https://ipgold.ru/api/v2` |
| Action selector | body param `action=<name>` |
| Auth | body param `api_key=<token>` |
| Success envelope | `{"status": "OK", ...payload}` |
| Error envelope | `{"status": "BAD", "errors": [{"message": "...", "code": <int>}, ...]}` |

This matches the **Perfect Panel** reseller API form, the same family as
`smmcode.shop` and `prskill.ru`. The adapter implementation models that shape.

## Known actions (Perfect Panel convention)

| Action | Required params | Returns |
|---|---|---|
| `balance` | — | `{balance: float, currency: str}` |
| `services` | — | array of `{service, name, type, category, rate, min, max, ...}` |
| `add` | `service`, `link`, `quantity` | `{order: int}` |
| `status` | `order` | `{charge, start_count, status, remains, currency}` |

`rate` is per **1000** units (Perfect Panel convention); the adapter divides by
1000 to compute per-unit price for the estimate-first cost cap.

## Known error codes

| code | meaning |
|---|---|
| 0 | Generic "Something is broken" wrapper. Usually accompanies another error. |
| 4 | `Action is not specified` — the `action` body param missing. |
| 403 | `Access denied` — see "Activation" below. |

## Activation

The personal API key must be **explicitly activated** in the user's IPGold
account before any action returns OK. Until activation:

- **Every** authenticated request returns `{"status": "BAD", "errors": [{"message": "Access denied", "code": 403}, ...]}`.
- This is independent of the action requested.

To enable the API for an account, the user must:

1. Sign in at https://ipgold.ru/
2. Navigate to the advertiser-side API docs page: https://ipgold.ru/api_docs
3. Enable API access on the profile / settings page (IPGold's UI; not via API).
4. *(On some plans)* whitelist the calling IP address. Check `2.26.110.148` is allowed if running from the bot's deployment.

After activation, the same `api_key` will start receiving `OK` envelopes.

## Adapter status

- `IpgoldAdapter` is a **PanelAdapter** (prepaid lifecycle, no per-submission
  accept/reject).
- Capabilities: `CREATE_ORDER`, `GET_ORDER_STATUS`, `GET_BALANCE`.
- Estimate-first cost cap enforced via cached `services` lookup before `/add`
  is called — same money-safety pattern as smmcode/prskill.
- The adapter surfaces IPGold's own error messages verbatim into `/balance` and
  `/health`, so the bot operator can immediately see why a call was rejected
  (typically: API not activated).

## Notes

- API tokens are read from `.env` (`IPGOLD_API_KEY`); never committed.
- The adapter's `_post` strips `api_key` from any error echo so it can't leak
  into bot replies, logs, or audit entries.

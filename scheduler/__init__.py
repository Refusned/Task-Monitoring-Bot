"""APScheduler jobs that run alongside the FastAPI app.

Three jobs, all kicked off every `verifier_poll_interval_seconds` (default 60s):

1. `poll_active_orders` — re-reads ACTIVE orders' status from their exchange
   and writes COMPLETED/FAILED transitions back to SQLite.
2. `verify_completed_orders` — for orders that just went COMPLETED but have
   no verification row yet, runs the matching PlatformVerifier (currently only
   YouTube) and writes the verdict + pushes a `report` event with the summary.
3. `recheck_balance_after_topup` — for each pending `topup_requests` row,
   re-reads that exchange's balance; if it crosses the requested amount we
   resolve the topup, publish an `topup_resolved` event, and (if
   `auto_resume_after_topup`) emit a `report` to nudge the user.

All jobs use `max_instances=1` and `coalesce=True` — if one heartbeat is still
running when the next fires, we skip it instead of stacking. Concurrent runs
would race on the same orders/topups; the safety-net for that is the
orchestrator's C1/C2 (which would catch a double, but cleaner to avoid).
"""

-- Schema for exchange-monitor-bot.
-- SQLite is the source of truth. WAL mode is enabled at connect time.
-- All tables use CREATE TABLE IF NOT EXISTS so this script is idempotent.
--
-- CHECK constraints (MEDIUM-e fix from Day 1 audit) enforce the invariants at
-- the DB layer as a defence-in-depth net: even if model validators are bypassed,
-- the DB refuses bad state.

CREATE TABLE IF NOT EXISTS migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_uuid TEXT PRIMARY KEY,
    external_order_id TEXT,
    exchange TEXT NOT NULL CHECK(length(exchange) > 0),
    scenario TEXT NOT NULL,
    target TEXT NOT NULL CHECK(length(target) > 0),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    service_id TEXT,
    source_platform TEXT,
    max_cost REAL NOT NULL CHECK(max_cost > 0),
    cost_actual REAL CHECK(cost_actual IS NULL OR cost_actual >= 0),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN (
        'draft','creating','active','verifying','completed','failed','cancelled'
    )),
    spec_json TEXT NOT NULL,
    raw_exchange_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(exchange, external_order_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS submissions (
    submission_uuid TEXT PRIMARY KEY,
    order_uuid TEXT NOT NULL REFERENCES orders(client_order_uuid),
    external_submission_id TEXT,
    executor_hint TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK(status IN (
        'new','verifying','awaiting_admin','accepting','accepted',
        'rejecting','rework_requested','failed'
    )),
    evidence TEXT,
    raw_exchange_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Day 3 audit (CRITICAL-1 fix): one local row per external submission per order.
    -- SQLite allows multiple NULLs in UNIQUE, so this only constrains non-null externals.
    UNIQUE(order_uuid, external_submission_id)
);

CREATE INDEX IF NOT EXISTS idx_submissions_order ON submissions(order_uuid);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);

CREATE TABLE IF NOT EXISTS payments (
    -- C2: terminal money decision per submission is UNIQUE.
    exchange TEXT NOT NULL,
    external_submission_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('accept','reject')),
    submission_uuid TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    PRIMARY KEY (exchange, external_submission_id)
);

CREATE TABLE IF NOT EXISTS action_log (
    -- C2: in-flight action record. Pattern: claim -> commit -> external call -> record.
    action_uuid TEXT PRIMARY KEY,
    submission_uuid TEXT,
    order_uuid TEXT,
    action TEXT NOT NULL CHECK(action IN ('create_order','accept','reject')),
    state TEXT NOT NULL CHECK(state IN ('in_progress','succeeded','failed')),
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS verifications (
    verification_uuid TEXT PRIMARY KEY,
    order_uuid TEXT,
    submission_uuid TEXT,
    verdict TEXT NOT NULL CHECK(verdict IN ('auto_pass','needs_human_review','fail')),
    measured REAL,
    expected REAL,
    reason TEXT,
    raw_evidence_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    order_uuid TEXT,
    submission_uuid TEXT,
    details_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_order ON audit_log(order_uuid);

CREATE TABLE IF NOT EXISTS report_rows (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    week TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    exchange TEXT NOT NULL,
    ordered_count INTEGER NOT NULL CHECK(ordered_count >= 0),
    actual_count INTEGER CHECK(actual_count IS NULL OR actual_count >= 0),
    cost REAL CHECK(cost IS NULL OR cost >= 0),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    order_uuid TEXT  -- added via ALTER TABLE on existing DBs (see init_db)
);

-- NOTE: The unique index on order_uuid is created in db.database.init_db AFTER
-- the ALTER TABLE migration so the column exists when the index is built. We
-- can't put it here because executescript would run it before the migration.

-- ===== Agent-layer tables (v4 pivot) =====

-- Last-known balance per exchange (best-effort cache).
-- Source of truth = the exchange itself via adapter.get_balance().
-- TTL is enforced in code (default 60s); we keep stale rows as fallback.
CREATE TABLE IF NOT EXISTS exchange_balance_cache (
    exchange TEXT PRIMARY KEY,
    amount REAL NOT NULL CHECK(amount >= 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    fetched_at TEXT NOT NULL
);

-- Pending topup hints emitted by the agent when no exchange has enough funds.
-- Resolved by APScheduler when the exchange's balance crosses requested_amount.
CREATE TABLE IF NOT EXISTS topup_requests (
    topup_uuid TEXT PRIMARY KEY,
    exchange TEXT NOT NULL,
    requested_amount REAL NOT NULL CHECK(requested_amount > 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    topup_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved','cancelled')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    user_chat_id INTEGER,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_topup_status ON topup_requests(status);

-- Baseline metric reading captured BEFORE an order is placed.
-- Used by the verifier to compute the delta after the order completes.
CREATE TABLE IF NOT EXISTS metric_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    target_url TEXT NOT NULL,
    metric TEXT NOT NULL,
    baseline_value REAL NOT NULL CHECK(baseline_value >= 0),
    captured_at TEXT NOT NULL,
    raw_json TEXT,
    order_uuid TEXT  -- linked when place_order succeeds
);

CREATE INDEX IF NOT EXISTS idx_snapshots_order ON metric_snapshots(order_uuid);

-- Live feed for the dashboard. Append-only.
CREATE TABLE IF NOT EXISTS agent_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(length(kind) > 0),
    payload_json TEXT NOT NULL,
    order_uuid TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_order ON agent_events(order_uuid);
CREATE INDEX IF NOT EXISTS idx_events_time ON agent_events(occurred_at DESC);

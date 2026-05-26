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
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_submissions_order ON submissions(order_uuid);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);

-- Day 3 audit (CRITICAL-1 fix): one local row per external submission per order.
-- Partial unique index (skips NULL external_submission_id) so two concurrent
-- pollers converge on a single canonical row via INSERT OR IGNORE in
-- ensure_submission_persisted(). Applies to fresh installs AND to the existing
-- live DB (CREATE INDEX IF NOT EXISTS is idempotent, unlike inline UNIQUE on a
-- pre-existing table).
CREATE UNIQUE INDEX IF NOT EXISTS uq_submissions_order_external
    ON submissions(order_uuid, external_submission_id)
    WHERE external_submission_id IS NOT NULL;

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
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watcher_state (
    account_url TEXT PRIMARY KEY,
    last_post_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

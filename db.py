"""Database schema and connection helper for the support ticket triage crew."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "tickets.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    payment_id TEXT,
    category TEXT CHECK (category IN ('billing', 'technical', 'refund')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'in_review', 'resolved', 'escalated')),
    resolution_action TEXT,
    resolution_notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    agent_name TEXT NOT NULL,
    step_type TEXT NOT NULL,
    input_summary TEXT,
    output_summary TEXT,
    reasoning TEXT,
    tokens_used INTEGER,
    cost_estimate REAL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_runs_ticket ON runs (ticket_id);
CREATE INDEX IF NOT EXISTS idx_runs_step_type ON runs (step_type);

CREATE TABLE IF NOT EXISTS action_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    action TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    external_id TEXT,
    detail TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_action_executions_ticket
    ON action_executions (ticket_id);

CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    customer_email TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'web',
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    kind TEXT NOT NULL DEFAULT 'message',
    body TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_support_messages_customer
    ON support_messages (customer_email, created_at, id);
CREATE INDEX IF NOT EXISTS idx_support_messages_ticket
    ON support_messages (ticket_id);

CREATE TABLE IF NOT EXISTS product_workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    product_url TEXT NOT NULL,
    github_repo TEXT NOT NULL DEFAULT '',
    github_branch TEXT NOT NULL DEFAULT 'main',
    linear_team_id TEXT NOT NULL DEFAULT '',
    linear_project_id TEXT NOT NULL DEFAULT '',
    bug_confidence_threshold TEXT NOT NULL DEFAULT 'likely'
        CHECK (bug_confidence_threshold IN ('likely', 'confirmed')),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    run_id TEXT NOT NULL,
    workspace_id INTEGER REFERENCES product_workspaces(id),
    product_context TEXT,
    code_evidence TEXT,
    verdict TEXT CHECK (verdict IN ('not_a_bug', 'inconclusive', 'likely', 'confirmed')),
    reasoning TEXT,
    linear_issue_id TEXT,
    linear_issue_url TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ticket_investigations_ticket
    ON ticket_investigations (ticket_id, created_at);

CREATE TABLE IF NOT EXISTS engineering_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    linear_issue_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    sent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (linear_issue_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_engineering_notifications_ticket
    ON engineering_notifications (ticket_id, sent_at);
"""


def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply the current schema and small additive migrations."""
    conn.executescript(SCHEMA)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()
    }
    if "payment_id" not in columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN payment_id TEXT")
    conn.commit()


def get_connection() -> sqlite3.Connection:
    """Return a connection to tickets.db with the schema applied."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    return conn

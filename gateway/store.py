"""
Gateway's own SQLite store — separate file from data/jobsearch.db on purpose.
This tracks who is allowed to call the gateway and what they have used, not
job-search data, and it needs to be readable/writable by a process that has
no other reason to touch the app's database.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from gateway.config import GATEWAY_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_clients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    token_prefix    TEXT NOT NULL,
    token_hash      TEXT NOT NULL,
    allowed_models  TEXT NOT NULL,           -- JSON list
    rpm_limit       INTEGER NOT NULL,
    daily_token_limit INTEGER NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    revoked_at      TEXT
);

CREATE TABLE IF NOT EXISTS gateway_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       INTEGER NOT NULL,
    model           TEXT NOT NULL,
    status          TEXT NOT NULL,           -- ok | error | rejected
    http_status     INTEGER,
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    upstream_key_prefix TEXT,
    latency_ms      INTEGER,
    error           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_client_time ON gateway_requests(client_id, created_at);

CREATE TABLE IF NOT EXISTS gateway_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(GATEWAY_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(GATEWAY_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- clients --

def create_client(name: str, token_prefix: str, token_hash: str,
                   allowed_models: list[str], rpm_limit: int,
                   daily_token_limit: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO gateway_clients
               (name, token_prefix, token_hash, allowed_models, rpm_limit,
                daily_token_limit, enabled, created_at)
               VALUES (?,?,?,?,?,?,1,?)""",
            (name, token_prefix, token_hash, json.dumps(allowed_models),
             rpm_limit, daily_token_limit, _now()),
        )
        return cur.lastrowid


def find_client_by_prefix(prefix: str) -> dict | None:
    """Candidates sharing a prefix (rare, 12 random chars) are all returned;
    the caller must still verify the full token hash against each."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM gateway_clients WHERE token_prefix = ? AND revoked_at IS NULL",
            (prefix,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_client(client_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM gateway_clients WHERE id = ?", (client_id,)).fetchone()
        return dict(row) if row else None


def list_clients() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM gateway_clients ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def set_client_enabled(client_id: int, enabled: bool) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE gateway_clients SET enabled = ? WHERE id = ?", (1 if enabled else 0, client_id)
        )
        return cur.rowcount > 0


def revoke_client(client_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE gateway_clients SET enabled = 0, revoked_at = ? WHERE id = ?",
            (_now(), client_id),
        )
        return cur.rowcount > 0


# --------------------------------------------------------------- requests --

def log_request(client_id: int, model: str, status: str, http_status: int | None,
                 prompt_tokens: int | None, completion_tokens: int | None,
                 upstream_key_prefix: str | None, latency_ms: int, error: str | None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO gateway_requests
               (client_id, model, status, http_status, prompt_tokens, completion_tokens,
                upstream_key_prefix, latency_ms, error, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (client_id, model, status, http_status, prompt_tokens, completion_tokens,
             upstream_key_prefix, latency_ms, error, _now()),
        )


def tokens_used_today(client_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT coalesce(sum(coalesce(prompt_tokens,0) + coalesce(completion_tokens,0)), 0) t
               FROM gateway_requests
               WHERE client_id = ? AND date(created_at) = date('now')""",
            (client_id,),
        ).fetchone()
        return row["t"]


def usage_stats(hours: int = 24) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.name, r.model,
                      count(*) n,
                      sum(CASE WHEN r.status='ok' THEN 1 ELSE 0 END) ok,
                      sum(CASE WHEN r.status='error' THEN 1 ELSE 0 END) errors,
                      sum(CASE WHEN r.status='rejected' THEN 1 ELSE 0 END) rejected,
                      sum(coalesce(r.prompt_tokens,0) + coalesce(r.completion_tokens,0)) tokens,
                      round(avg(r.latency_ms)) avg_latency_ms
               FROM gateway_requests r JOIN gateway_clients c ON c.id = r.client_id
               WHERE r.created_at >= datetime('now', ?)
               GROUP BY c.name, r.model
               ORDER BY n DESC""",
            (f"-{hours} hours",),
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------- settings --

def get_setting(key: str, default: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM gateway_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO gateway_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )


def global_enabled() -> bool:
    return get_setting("global_enabled", "1") == "1"


def set_global_enabled(enabled: bool):
    set_setting("global_enabled", "1" if enabled else "0")

import sqlite3
import os
import json
from datetime import datetime, timedelta
from contextlib import contextmanager
from app.config import LOCAL_DB_PATH

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sqlite.sql")


def _ensure_schema(conn):
    with open(_SCHEMA_PATH) as f:
        conn.executescript(f.read())


@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(LOCAL_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(LOCAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def fetch_companies(status: str | None = None, limit: int | None = None):
    query = "SELECT id, name, website, careers_url FROM companies"
    params = []
    if status:
        query += " WHERE scrape_status = ?"
        params.append(status)
    query += " ORDER BY id"
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    with get_conn() as conn:
        cur = conn.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def upsert_jobs(company_id: int, jobs: list[dict]):
    with get_conn() as conn:
        for job in jobs:
            conn.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (company_id, title, location, remote, salary, description, job_url, posted_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_id,
                    job.get("title"),
                    job.get("location"),
                    1 if job.get("remote") else 0,
                    job.get("salary"),
                    job.get("description"),
                    job.get("job_url"),
                    job.get("posted_date"),
                ),
            )


def set_careers_url(company_id: int, careers_url: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE companies SET careers_url = ? WHERE id = ?", (careers_url, company_id)
        )


def mark_company_status(company_id: int, status: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE companies SET scrape_status = ?, last_scraped_at = datetime('now') WHERE id = ?",
            (status, company_id),
        )


def log_scrape_run(company_id: int, status: str, jobs_found: int, error: str | None = None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO scrape_runs (company_id, status, jobs_found, error, finished_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (company_id, status, jobs_found, error),
        )


def get_method(company_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM scrape_methods WHERE company_id = ?", (company_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["method_config"] = json.loads(d["method_config"]) if d["method_config"] else {}
        return d


def save_method(company_id: int, method_type: str, method_config: dict):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO scrape_methods (company_id, method_type, method_config, last_success_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(company_id) DO UPDATE SET
                method_type = excluded.method_type,
                method_config = excluded.method_config,
                last_success_at = datetime('now'),
                consecutive_failures = 0
            """,
            (company_id, method_type, json.dumps(method_config)),
        )


def record_method_result(company_id: int, success: bool):
    with get_conn() as conn:
        if success:
            conn.execute(
                "UPDATE scrape_methods SET last_success_at = datetime('now'), consecutive_failures = 0 WHERE company_id = ?",
                (company_id,),
            )
        else:
            conn.execute(
                "UPDATE scrape_methods SET consecutive_failures = consecutive_failures + 1 WHERE company_id = ?",
                (company_id,),
            )


def start_daily_run():
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO daily_runs DEFAULT VALUES")
        return cur.lastrowid


def finish_daily_run(run_id: int, attempted: int, deterministic: int, ai: int, failed: int, jobs_found: int):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE daily_runs SET
                finished_at = datetime('now'),
                companies_attempted = ?,
                companies_deterministic = ?,
                companies_ai = ?,
                companies_failed = ?,
                jobs_found = ?
            WHERE id = ?
            """,
            (attempted, deterministic, ai, failed, jobs_found, run_id),
        )


def readiness_stats():
    with get_conn() as conn:
        figured_out = conn.execute("SELECT count(*) FROM scrape_methods").fetchone()[0]
        by_type = conn.execute(
            "SELECT method_type, count(*) c FROM scrape_methods GROUP BY method_type"
        ).fetchall()
        total_companies = conn.execute("SELECT count(*) FROM companies").fetchone()[0]
        last_run = conn.execute(
            "SELECT * FROM daily_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "total_companies": total_companies,
        "figured_out": figured_out,
        "pending_ai_discovery": total_companies - figured_out,
        "by_type": {r["method_type"]: r["c"] for r in by_type},
        "last_run": dict(last_run) if last_run else None,
    }


def job_stats():
    cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
    with get_conn() as conn:
        total_companies = conn.execute("SELECT count(*) FROM companies").fetchone()[0]
        scraped = conn.execute(
            "SELECT count(*) FROM companies WHERE scrape_status = 'done'"
        ).fetchone()[0]
        total_jobs = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
        recent_jobs = conn.execute(
            "SELECT count(*) FROM jobs WHERE posted_date >= ?", (cutoff,)
        ).fetchone()[0]
    return {
        "total_companies": total_companies,
        "scraped": scraped,
        "total_jobs": total_jobs,
        "recent_jobs": recent_jobs,
    }

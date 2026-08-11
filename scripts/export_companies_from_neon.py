"""
One-time export: pull company records from the Talentos Neon DB and
load them into the local SQLite DB as seed data.
Run: python -m scripts.export_companies_from_neon
"""
import psycopg
from app import db
from app.config import NEON_DB_URL

NEON_QUERY = """
SELECT id, name, website, address
FROM companies
"""


def main():
    if not NEON_DB_URL:
        raise SystemExit("NEON_DB_URL not set in .env")

    print("Connecting to Neon...")
    with psycopg.connect(NEON_DB_URL) as src_conn:
        with src_conn.cursor() as cur:
            cur.execute(NEON_QUERY)
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]

    print(f"Fetched {len(rows)} companies from Neon.")

    with db.get_conn() as conn:
        for row in rows:
            data = dict(zip(cols, row))
            address = data.get("address") or {}
            location = ", ".join(
                str(v) for v in [address.get("city"), address.get("country")] if v
            ) or None
            conn.execute(
                """
                INSERT INTO companies (source_id, name, website, location)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(data.get("id")),
                    data.get("name"),
                    data.get("website"),
                    location,
                ),
            )

    print(f"Loaded {len(rows)} companies into local DB.")


if __name__ == "__main__":
    main()

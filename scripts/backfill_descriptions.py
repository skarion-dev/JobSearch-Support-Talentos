"""
Backfill full job descriptions (and evidence-verified posted dates) for jobs
that matched a candidate profile.

Scope: matched jobs only by default. Enriching a job nobody matched has no
value, and it is the volume that breaks things.

Concurrency: uses the HTTP-fetch + deepseek-v4-flash agent, no browser, so this
runs 100-wide. The previous Playwright-based version capped at ~8 and crashed
at 50 with EPIPE — the bottleneck was Chromium, never the model.

Resumable: each job is written as it completes, and jobs that already have a
full description are skipped, so re-running continues where it stopped.

Run: python -m scripts.backfill_descriptions --workers 100
"""
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db
from app.agents.description_agent import extract, MIN_DESCRIPTION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backfill_descriptions")


def load_targets(limit: int | None, matched_only: bool) -> list[dict]:
    query = (
        "SELECT id, source_url FROM keyword_jobs "
        "WHERE source_url IS NOT NULL AND length(coalesce(description,'')) < ?"
    )
    if matched_only:
        query += " AND id IN (SELECT DISTINCT keyword_job_id FROM resume_job_matches)"
    if limit:
        query += f" LIMIT {int(limit)}"
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(query, (MIN_DESCRIPTION,)).fetchall()]


def _process_one(row: dict) -> tuple[bool, bool]:
    desc, posted = extract(row["source_url"])
    if not desc and not posted:
        return False, False
    with db.get_conn() as conn:
        if desc:
            conn.execute(
                "UPDATE keyword_jobs SET description = ? WHERE id = ?", (desc, row["id"])
            )
        if posted:
            conn.execute(
                "UPDATE keyword_jobs SET posted_date = ? WHERE id = ?", (posted, row["id"])
            )
    return bool(desc), bool(posted)


def main(limit: int | None, workers: int, matched_only: bool):
    rows = load_targets(limit, matched_only)
    scope = "matched jobs" if matched_only else "ENTIRE corpus"
    log.info(f"Enriching {len(rows)} jobs ({scope}) with {workers} workers")
    if not rows:
        return

    upgraded = dated = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process_one, r): r for r in rows}
        for i, future in enumerate(as_completed(futures), 1):
            try:
                got_desc, got_date = future.result()
            except Exception:
                got_desc = got_date = False
            upgraded += got_desc
            dated += got_date
            if i % 50 == 0:
                log.info(f"  {i}/{len(rows)} | {upgraded} descriptions, {dated} verified dates")

    log.info(f"Done. {upgraded}/{len(rows)} full descriptions, {dated} evidence-backed dates")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=100)
    parser.add_argument("--all", action="store_true", help="Whole corpus, not just matched jobs")
    args = parser.parse_args()
    main(limit=args.limit, workers=args.workers, matched_only=not args.all)

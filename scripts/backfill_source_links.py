"""
Backfill source_url for keyword_jobs rows that don't have one yet, using
web search (title + company) since Adzuna's own links are gated.

Run: python -m scripts.backfill_source_links --limit 6942 --workers 20
"""
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db
from app.agents.link_finder import find_source_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backfill_source_links")


def _process_one(row: dict) -> tuple[int, str | None]:
    url = find_source_url(row["title"], row["company_name"])
    if url:
        db.set_keyword_job_source_url(row["id"], url)
    return row["id"], url


def main(limit: int | None, workers: int, matched_only: bool = True):
    rows = db.fetch_keyword_jobs_missing_source(limit=limit, matched_only=matched_only)
    scope = "matched jobs only" if matched_only else "ENTIRE corpus"
    log.info(f"Backfilling source_url for {len(rows)} jobs ({scope}), {workers} workers")

    found = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process_one, r): r for r in rows}
        for future in as_completed(futures):
            job_id, url = future.result()
            if url:
                found += 1

    log.info(f"Done. {found}/{len(rows)} jobs got a source_url")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Max jobs to process")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent search workers (a global throttle paces them)")
    parser.add_argument("--all", action="store_true", help="Enrich the whole corpus instead of just matched jobs")
    args = parser.parse_args()
    main(limit=args.limit, workers=args.workers, matched_only=not args.all)

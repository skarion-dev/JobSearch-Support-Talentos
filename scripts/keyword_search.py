"""
Search Adzuna for the top N job-search keywords (from data/keywords.csv,
ranked by occurrence count), across the entire USA, within the last N days.
Not tied to the companies table — general keyword-driven job discovery.

Adzuna's free tier caps at 250 calls/day, so keep top_n * pages_per_keyword <= 250.

Run: python -m scripts.keyword_search --top 250 --days 3 --workers 20
"""
import argparse
import csv
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db
from app.agents.aggregators.adzuna import fetch_by_keyword, to_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("keyword_search")

KEYWORDS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "keywords.csv")


def load_top_keywords(n: int) -> list[str]:
    keywords = []
    with open(KEYWORDS_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keywords.append(row["keyword"])
    return keywords[:n]


def _search_one(keyword: str, max_days_old: int) -> tuple[str, int]:
    try:
        results = fetch_by_keyword(keyword, max_days_old=max_days_old)
    except Exception as e:
        log.warning(f"{keyword}: FAILED {e}")
        return keyword, 0

    jobs = [to_job(r) for r in results]
    inserted = db.upsert_keyword_jobs(keyword, jobs)
    return keyword, inserted


def main(top_n: int, days: int, workers: int):
    keywords = load_top_keywords(top_n)
    log.info(f"Searching {len(keywords)} keywords, last {days} days, {workers} parallel workers")

    total_inserted = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_search_one, kw, days): kw for kw in keywords}
        for future in as_completed(futures):
            keyword, inserted = future.result()
            total_inserted += inserted
            log.info(f"{keyword}: {inserted} new jobs")

    log.info(f"Done. {total_inserted} new jobs across {len(keywords)} keywords")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=250, help="Number of top keywords to search")
    parser.add_argument("--days", type=int, default=3, help="Max days old for postings")
    parser.add_argument("--workers", type=int, default=20, help="Concurrent search workers")
    args = parser.parse_args()
    main(top_n=args.top, days=args.days, workers=args.workers)

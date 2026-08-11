"""
Search Adzuna for the top N job-search keywords (from data/keywords.csv,
ranked by occurrence count), across the entire USA, within the last N days.
Fully paginates each keyword (up to --max-pages) instead of capping at 50
results. Not tied to the companies table — general keyword-driven job
discovery, stored locally in keyword_jobs.

Adzuna's free tier caps at 250 calls/day. This script stops issuing new
calls once --call-budget is reached (default 250) — already-inflight
keywords finish, but no new ones start.

Run: python -m scripts.keyword_search --top 250 --days 3 --workers 20 --max-pages 3
"""
import argparse
import csv
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db
from app.agents.aggregators.adzuna import fetch_by_keyword_all, to_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("keyword_search")

KEYWORD_SOURCES = {
    # Ranked by relevance to the 19 ACTIVE base-resume profiles (preferred)
    "profile": os.path.join(os.path.dirname(__file__), "..", "data", "profile_keywords.csv"),
    # Raw historical export, ranked by occurrence across all sources
    "export": os.path.join(os.path.dirname(__file__), "..", "data", "keywords.csv"),
}

_call_lock = threading.Lock()
_calls_used = 0


def load_top_keywords(n: int, source: str = "profile") -> list[str]:
    path = KEYWORD_SOURCES[source]
    with open(path, encoding="utf-8") as f:
        keywords = [row["keyword"] for row in csv.DictReader(f)]
    return keywords[:n]


def _search_one(keyword: str, max_days_old: int, max_pages: int, call_budget: int) -> tuple[str, int, int]:
    global _calls_used
    with _call_lock:
        if _calls_used >= call_budget:
            return keyword, 0, 0
        remaining = call_budget - _calls_used

    try:
        results, calls_made = fetch_by_keyword_all(
            keyword, max_days_old=max_days_old, max_pages=min(max_pages, remaining)
        )
    except Exception as e:
        log.warning(f"{keyword}: FAILED {e}")
        return keyword, 0, 1

    with _call_lock:
        _calls_used += calls_made

    jobs = [to_job(r) for r in results]
    inserted = db.upsert_keyword_jobs(keyword, jobs)
    return keyword, inserted, calls_made


def main(top_n: int, days: int, workers: int, max_pages: int, call_budget: int, source: str = "profile"):
    keywords = load_top_keywords(top_n, source=source)
    log.info(f"Keyword source: {source} ({KEYWORD_SOURCES[source]})")
    log.info(
        f"Searching {len(keywords)} keywords, last {days} days, {workers} workers, "
        f"up to {max_pages} pages/keyword, call budget {call_budget}"
    )

    total_inserted = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_search_one, kw, days, max_pages, call_budget): kw for kw in keywords
        }
        for future in as_completed(futures):
            keyword, inserted, calls = future.result()
            total_inserted += inserted
            if calls:
                log.info(f"{keyword}: {inserted} new jobs ({calls} calls)")

    log.info(f"Done. {total_inserted} new jobs, {_calls_used} Adzuna calls used")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=250, help="Number of top keywords to search")
    parser.add_argument("--days", type=int, default=3, help="Max days old for postings")
    parser.add_argument("--workers", type=int, default=20, help="Concurrent search workers")
    parser.add_argument("--max-pages", type=int, default=3, help="Max pages to paginate per keyword")
    parser.add_argument("--call-budget", type=int, default=250, help="Total Adzuna API calls allowed this run")
    parser.add_argument(
        "--source", choices=["profile", "export"], default="profile",
        help="profile = ranked against the 19 active base resumes (default); export = raw historical list",
    )
    args = parser.parse_args()
    main(
        top_n=args.top, days=args.days, workers=args.workers,
        max_pages=args.max_pages, call_budget=args.call_budget, source=args.source,
    )

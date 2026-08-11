"""
Daily scrape run: intended to be scheduled at 6AM.

For every company:
  - if we've already figured out a deterministic method (Greenhouse/Lever/Workday
    API, or a cached CSS approach), use it — free, fast, no LLM call.
  - otherwise, fall back to the finder + AI scraper agent, and if the discovered
    page matches a known ATS, the method gets cached for all future runs.

Run manually:  python -m scripts.daily_scrape
Run a subset:  python -m scripts.daily_scrape --limit 50
"""
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db
from app.agents.scraper_agent import scrape_company

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("daily_scrape")


def _run_one(company: dict) -> dict:
    try:
        return scrape_company(company)
    except Exception as e:
        db.mark_company_status(company["id"], "error")
        db.log_scrape_run(company["id"], "error", 0, error=str(e))
        return {"company_id": company["id"], "status": "error", "jobs": 0, "error": str(e)}


def main(limit: int | None = None, only_pending: bool = False, workers: int = 20):
    purged = db.purge_old_jobs(days=10)
    if purged:
        log.info(f"Purged {purged} jobs older than 10 days")

    status_filter = "pending" if only_pending else None
    companies = db.fetch_companies(status=status_filter, limit=limit)
    log.info(f"Starting daily scrape run for {len(companies)} companies with {workers} workers")

    run_id = db.start_daily_run()
    attempted = deterministic = ai = failed = jobs_found = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_run_one, c): c for c in companies}
        for future in as_completed(futures):
            company = futures[future]
            result = future.result()

            attempted += 1
            via = result.get("via")
            if result["status"] == "done":
                jobs_found += result.get("jobs", 0)
                if via == "ai":
                    ai += 1
                else:
                    deterministic += 1
            else:
                failed += 1

            log.info(f"{company['name']}: {result['status']} via={via} jobs={result.get('jobs', 0)}")

    db.finish_daily_run(run_id, attempted, deterministic, ai, failed, jobs_found)
    log.info(
        f"Done. attempted={attempted} deterministic={deterministic} ai={ai} "
        f"failed={failed} jobs_found={jobs_found}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max companies to process (omit or negative for no limit)",
    )
    parser.add_argument("--only-pending", action="store_true", help="Only companies never scraped before")
    parser.add_argument("--workers", type=int, default=20, help="Concurrent scrape workers")
    args = parser.parse_args()
    limit = None if args.limit is None or args.limit < 0 else args.limit
    main(limit=limit, only_pending=args.only_pending, workers=args.workers)

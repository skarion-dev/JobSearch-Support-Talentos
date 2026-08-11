"""
Daily scrape run: intended to be scheduled at 6AM.

Aggregator-first architecture (scales to millions of companies):
  1. Pull recent US postings in bulk from Adzuna (a handful of API calls
     surface postings for many companies at once) and match each posting's
     employer name against the companies table.
  2. Optionally (--fallback), for companies the aggregator didn't cover,
     fall back to the per-company ATS-detector/AI scraper path. This does
     NOT scale to millions of companies run daily — use sparingly, e.g. a
     capped --limit, or only for a priority subset.

Run manually:       python -m scripts.daily_scrape
Aggregator only:     python -m scripts.daily_scrape --pages 40
With fallback:       python -m scripts.daily_scrape --fallback --limit 200
"""
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db
from app.agents.scraper_agent import scrape_company
from app.agents.aggregators.adzuna import fetch_and_match, normalize_company_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("daily_scrape")


def run_aggregator_pass(max_pages: int = 20, max_days_old: int = 10) -> tuple[int, int, set[int]]:
    companies = db.fetch_companies()
    index = {}
    for c in companies:
        key = normalize_company_name(c["name"])
        if key:
            index[key] = c["id"]

    matched_companies: set[int] = set()
    jobs_found = 0

    for company_id, job in fetch_and_match(index, max_pages=max_pages, max_days_old=max_days_old):
        db.upsert_jobs(company_id, [job])
        db.mark_company_status(company_id, "done")
        matched_companies.add(company_id)
        jobs_found += 1

    for company_id in matched_companies:
        db.log_scrape_run(company_id, "done", 1)

    return len(matched_companies), jobs_found, matched_companies


def _run_one(company: dict) -> dict:
    try:
        return scrape_company(company)
    except Exception as e:
        db.mark_company_status(company["id"], "error")
        db.log_scrape_run(company["id"], "error", 0, error=str(e))
        return {"company_id": company["id"], "status": "error", "jobs": 0, "error": str(e)}


def run_fallback_pass(skip_ids: set[int], limit: int | None, workers: int) -> tuple[int, int, int, int]:
    companies = db.fetch_companies(status="pending", limit=None)
    companies = [c for c in companies if c["id"] not in skip_ids]
    if limit:
        companies = companies[:limit]

    attempted = deterministic = ai = failed = 0
    jobs_found = 0

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
            log.info(f"[fallback] {company['name']}: {result['status']} via={via} jobs={result.get('jobs', 0)}")

    return attempted, deterministic + ai, failed, jobs_found


def main(pages: int = 20, use_fallback: bool = False, limit: int | None = None, workers: int = 20):
    purged = db.purge_old_jobs(days=10)
    if purged:
        log.info(f"Purged {purged} jobs older than 10 days")

    run_id = db.start_daily_run()

    log.info(f"Starting aggregator pass ({pages} pages from Adzuna)")
    agg_companies, agg_jobs, matched_ids = run_aggregator_pass(max_pages=pages)
    log.info(f"Aggregator matched {agg_companies} companies, {agg_jobs} jobs")

    fb_attempted = fb_done = fb_failed = fb_jobs = 0
    if use_fallback:
        log.info("Starting fallback pass for companies the aggregator missed")
        fb_attempted, fb_done, fb_failed, fb_jobs = run_fallback_pass(matched_ids, limit, workers)
        log.info(f"Fallback: attempted={fb_attempted} done={fb_done} failed={fb_failed} jobs={fb_jobs}")

    db.finish_daily_run(
        run_id,
        attempted=agg_companies + fb_attempted,
        deterministic=agg_companies,
        ai=fb_done,
        failed=fb_failed,
        jobs_found=agg_jobs + fb_jobs,
    )
    log.info(f"Done. total_jobs_found={agg_jobs + fb_jobs}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=20, help="Adzuna pages to pull (50 results/page)")
    parser.add_argument("--fallback", action="store_true", help="Also run per-company ATS/AI scraping for unmatched companies")
    parser.add_argument("--limit", type=int, default=None, help="Cap on fallback companies processed")
    parser.add_argument("--workers", type=int, default=20, help="Concurrent fallback scrape workers")
    args = parser.parse_args()
    main(pages=args.pages, use_fallback=args.fallback, limit=args.limit, workers=args.workers)

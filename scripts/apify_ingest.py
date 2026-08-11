"""
Ingest jobs via Apify for the boards a plain HTTP fetch cannot read.

Stores EVERY identifier Talentos dedupes on, so nothing we push can create a
duplicate there (masterprompt rule 7 order: external_job_id > ref_id >
source_url / apply_url > normalized company+title+location):

    external_job_id   platform's own job id (strongest key)
    job_url           canonical posting URL
    apply_url         resolved application URL
    source            'apify:linkedin' | 'apify:indeed' | 'adzuna' | ...
    company_url       employer page, useful for company reconciliation

Descriptions arrive full from the actor, so these rows need no enrichment pass.

Run: python -m scripts.apify_ingest --source linkedin --top 60 --max-items 500
"""
import argparse
import csv
import logging
import os

from app import db
from app.agents.aggregators.apify_jobs import run_actor, token_status
from app.filters import filter_us_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("apify_ingest")

ROI_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "roi_keywords.csv")


def load_keywords(n: int) -> list[str]:
    with open(ROI_PATH, encoding="utf-8") as f:
        return [r["keyword"] for r in csv.DictReader(f)][:n]


def store(rows: list[dict], keyword_label: str) -> tuple[int, int]:
    """Insert, deduping on external_job_id first then job_url. Returns (new, dupe)."""
    new = dupe = 0
    with db.get_conn() as conn:
        for j in rows:
            ext = j.get("external_job_id")
            if ext:
                hit = conn.execute(
                    "SELECT 1 FROM keyword_jobs WHERE external_job_id = ? LIMIT 1", (ext,)
                ).fetchone()
                if hit:
                    dupe += 1
                    continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO keyword_jobs
                    (keyword, title, company_name, location, salary, description,
                     job_url, source_url, apply_url, external_job_id, company_url,
                     source, posted_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    keyword_label, j.get("title"), j.get("company_name"), j.get("location"),
                    j.get("salary"), j.get("description"),
                    j.get("job_url"), j.get("job_url"), j.get("apply_url"),
                    j.get("external_job_id"), j.get("company_url"),
                    j.get("source"), j.get("posted_date"),
                ),
            )
            if cur.rowcount:
                new += 1
            else:
                dupe += 1
    return new, dupe


def main(source: str, top: int, max_items: int, days: int, chunk: int):
    keywords = load_keywords(top)
    log.info(f"{source}: {len(keywords)} keywords, max {max_items}/run, last {days}d")
    log.info(f"tokens: {token_status()}")

    total_new = total_dupe = 0
    for i in range(0, len(keywords), chunk):
        batch = keywords[i : i + chunk]
        log.info(f"  run {i//chunk + 1}: {len(batch)} keywords")
        try:
            rows = run_actor(source, batch, max_items=max_items, days=days)
        except Exception as e:
            log.warning(f"  actor run failed: {e}")
            continue

        us_rows = filter_us_jobs(rows)
        new, dupe = store(us_rows, keyword_label=f"apify:{source}")
        total_new += new
        total_dupe += dupe
        log.info(f"    {len(rows)} rows -> {len(us_rows)} US -> {new} new, {dupe} dupe")

    log.info(f"Done. {total_new} new jobs, {total_dupe} deduped. tokens: {token_status()}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", choices=["linkedin", "indeed", "google", "indeed_alt"], default="linkedin")
    p.add_argument("--top", type=int, default=60, help="Top-N ROI keywords to search")
    p.add_argument("--max-items", type=int, default=500)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--chunk", type=int, default=0,
                   help="Keywords per actor run (0 = per-actor default)")
    a = p.parse_args()
    # Indeed's query parser returns nothing for OR-joined lists, so it must be
    # driven one keyword at a time. LinkedIn/Google accept a whole batch.
    default_chunk = {"indeed": 1, "indeed_alt": 1, "google": 6}.get(a.source, 60)
    main(a.source, a.top, a.max_items, a.days, a.chunk or default_chunk)

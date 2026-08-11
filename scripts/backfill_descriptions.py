"""
Backfill full job descriptions for keyword_jobs rows that have a source_url
(found via link_finder) but only a truncated ~500-char Adzuna snippet.
Uses the AI scraper against the real source page.

Run: python -m scripts.backfill_descriptions --workers 50
"""
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from scrapegraphai.graphs import SmartScraperGraph
from app.config import LLM_CONFIG
from app import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backfill_descriptions")

PROMPT = (
    "Extract from this job posting page: "
    "1) The FULL job description text — all responsibilities, requirements, and "
    "qualifications, not a summary or truncated snippet. "
    "2) The date this job was posted, ONLY if there is strong, explicit evidence "
    "on the page (an actual 'Posted on', 'Date posted', or similar label with a "
    "real date, or an unambiguous relative date like 'Posted 2 days ago' next to "
    "today's context). Do NOT guess, infer, or use a scrape/crawl date. If there "
    "is no clear posted-date evidence, return null for it. "
    "Return JSON with keys: 'description' (string or null), 'posted_date' "
    "(string in YYYY-MM-DD format or null), and 'posted_date_evidence' (a short "
    "quote of the text that shows the date, or null)."
)

GRAPH_CONFIG = {"llm": LLM_CONFIG, "verbose": False, "headless": True}

# Adzuna snippets cap out around 500 chars; treat anything shorter as "still truncated"
TRUNCATED_LEN_THRESHOLD = 550


def fetch_full_description(url: str) -> tuple[str | None, str | None]:
    """Returns (description, posted_date). posted_date is only set with explicit evidence."""
    try:
        graph = SmartScraperGraph(prompt=PROMPT, source=url, config=GRAPH_CONFIG)
        result = graph.run()
        if not isinstance(result, dict):
            return None, None

        desc = result.get("description")
        desc = desc if desc and len(desc) > TRUNCATED_LEN_THRESHOLD else None

        posted_date = result.get("posted_date")
        evidence = result.get("posted_date_evidence")
        # Only trust a posted_date if the model also gave concrete evidence for it
        if not evidence or not posted_date:
            posted_date = None

        return desc, posted_date
    except Exception:
        return None, None


def _process_one(row: dict) -> tuple[int, bool, bool]:
    full_desc, posted_date = fetch_full_description(row["source_url"])
    if full_desc or posted_date:
        with db.get_conn() as conn:
            if full_desc:
                conn.execute(
                    "UPDATE keyword_jobs SET description = ? WHERE id = ?", (full_desc, row["id"])
                )
            if posted_date:
                conn.execute(
                    "UPDATE keyword_jobs SET posted_date = ? WHERE id = ?", (posted_date, row["id"])
                )
    return row["id"], bool(full_desc), bool(posted_date)


def main(limit: int | None, workers: int):
    with db.get_conn() as conn:
        query = (
            "SELECT id, source_url FROM keyword_jobs "
            "WHERE source_url IS NOT NULL AND length(description) < ?"
        )
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = [dict(r) for r in conn.execute(query, (TRUNCATED_LEN_THRESHOLD,)).fetchall()]

    log.info(f"Backfilling full descriptions for {len(rows)} jobs, {workers} parallel workers")

    upgraded = dated = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process_one, r): r for r in rows}
        for i, future in enumerate(as_completed(futures), 1):
            job_id, got_desc, got_date = future.result()
            if got_desc:
                upgraded += 1
            if got_date:
                dated += 1
            if i % 25 == 0:
                log.info(f"Progress: {i}/{len(rows)} processed, {upgraded} descriptions, {dated} dates")

    log.info(f"Done. {upgraded}/{len(rows)} jobs got a full description, {dated} got a verified posted_date")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=50)
    args = parser.parse_args()
    main(limit=args.limit, workers=args.workers)

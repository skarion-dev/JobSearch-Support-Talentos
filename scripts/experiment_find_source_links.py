"""
Experiment: can the AI scraper find the real company application link
behind an Adzuna redirect/landing page? Tests on a sample of already-stored
keyword_jobs rows.

Run: python -m scripts.experiment_find_source_links --sample 20
"""
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from scrapegraphai.graphs import SmartScraperGraph
from app.config import LLM_CONFIG
from app import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("experiment")

PROMPT = (
    "This is a job listing landing page (likely from an aggregator like Adzuna). "
    "Find the link/button that leads to the ORIGINAL employer's job posting or "
    "application page (often labeled 'Apply', 'Apply Now', 'View original', or "
    "similar, and usually points to a different domain than the current page). "
    "Return JSON with keys: source_url (the href of that link, or null if not found), "
    "source_domain (the domain name of that link, or null), and found (true/false)."
)

GRAPH_CONFIG = {"llm": LLM_CONFIG, "verbose": False, "headless": True}


def try_one(row: dict) -> dict:
    try:
        graph = SmartScraperGraph(prompt=PROMPT, source=row["job_url"], config=GRAPH_CONFIG)
        result = graph.run()
        if not isinstance(result, dict):
            return {**row, "found": False, "source_url": None, "source_domain": None, "error": "bad result"}
        return {
            **row,
            "found": result.get("found", False),
            "source_url": result.get("source_url"),
            "source_domain": result.get("source_domain"),
            "error": None,
        }
    except Exception as e:
        return {**row, "found": False, "source_url": None, "source_domain": None, "error": str(e)}


def main(sample: int, workers: int):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, keyword, title, company_name, job_url FROM keyword_jobs "
            "WHERE job_url IS NOT NULL ORDER BY RANDOM() LIMIT ?",
            (sample,),
        ).fetchall()
    rows = [dict(r) for r in rows]

    log.info(f"Testing {len(rows)} sample job URLs with {workers} workers")

    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(try_one, r): r for r in rows}
        for future in as_completed(futures):
            res = future.result()
            results.append(res)
            status = "FOUND" if res["found"] else "not found"
            log.info(f"{res['title']} @ {res['company_name']}: {status} -> {res['source_url']} ({res['error'] or ''})")

    found_count = sum(1 for r in results if r["found"])
    log.info(f"Done. {found_count}/{len(results)} found a source link")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    main(sample=args.sample, workers=args.workers)

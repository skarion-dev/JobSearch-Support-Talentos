"""
Experiment: instead of scraping Adzuna's gated landing page, search the web
directly for "<title> <company>" and see if we can find the real job
posting (company career page, LinkedIn, Indeed, etc.) among the top results.

Run: python -m scripts.experiment_google_search --sample 20
"""
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from ddgs import DDGS

from app import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("experiment")

BAD_DOMAINS = ("adzuna.com",)


def search_one(row: dict) -> dict:
    query = f'"{row["title"]}" {row["company_name"]}'
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
    except Exception as e:
        return {**row, "found": False, "match_url": None, "error": str(e)}

    for r in results:
        url = r.get("href") or r.get("url") or ""
        if url and not any(bad in url.lower() for bad in BAD_DOMAINS):
            return {**row, "found": True, "match_url": url, "error": None}

    return {**row, "found": False, "match_url": None, "error": None}


def main(sample: int, workers: int):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, keyword, title, company_name, job_url FROM keyword_jobs "
            "WHERE job_url IS NOT NULL AND company_name IS NOT NULL ORDER BY RANDOM() LIMIT ?",
            (sample,),
        ).fetchall()
    rows = [dict(r) for r in rows]

    log.info(f"Searching {len(rows)} sample jobs via web search, {workers} workers")

    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(search_one, r): r for r in rows}
        for future in as_completed(futures):
            res = future.result()
            results.append(res)
            status = "FOUND" if res["found"] else "not found"
            log.info(f"{res['title']} @ {res['company_name']}: {status} -> {res['match_url']} ({res['error'] or ''})")

    found_count = sum(1 for r in results if r["found"])
    log.info(f"Done. {found_count}/{len(results)} found a plausible direct link")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    main(sample=args.sample, workers=args.workers)

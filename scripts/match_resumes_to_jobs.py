"""
Run the resume-to-job matcher across all synced resume profiles against
recently-captured keyword_jobs. Report-only: writes only to local
resume_job_matches (never to Talentos). Meant to run after each daily
keyword_search/scrape pass ("suggested logs after everyday jobs are captured").

Run: python -m scripts.match_resumes_to_jobs --top 50 --workers 6
"""
import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db
from app.agents.matcher_agent import match_profile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("match_resumes")


def load_profiles() -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM resume_profiles").fetchall()]


def load_recent_jobs(days: int) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, title, company_name, location, description, posted_date
            FROM keyword_jobs
            WHERE scraped_at >= datetime('now', ?)
            """,
            (f"-{days} days",),
        ).fetchall()
        return [dict(r) for r in rows]


def _run_one_profile(profile: dict, jobs: list[dict], top_n: int, run_id: str) -> tuple[str, int]:
    matches = match_profile(profile, jobs, top_n=top_n)
    with db.get_conn() as conn:
        for m in matches:
            conn.execute(
                """
                INSERT INTO resume_job_matches
                    (resume_profile_id, keyword_job_id, score, band, reason, matched_terms, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resume_profile_id, keyword_job_id) DO UPDATE SET
                    score = excluded.score, band = excluded.band, reason = excluded.reason,
                    matched_terms = excluded.matched_terms, run_id = excluded.run_id,
                    matched_at = datetime('now')
                """,
                (
                    profile["id"], m["job_id"], m["score"], m["band"], m["reason"],
                    json.dumps(m["matched_terms"]), run_id,
                ),
            )
    return profile["base_resume_name"], len(matches)


def main(top_n: int, days: int, workers: int):
    profiles = load_profiles()
    jobs = load_recent_jobs(days)
    log.info(f"Matching {len(profiles)} resume profiles against {len(jobs)} recent jobs (last {days} days)")

    import time
    run_id = f"run_{int(time.time())}"

    total_matches = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_run_one_profile, p, jobs, top_n, run_id): p for p in profiles
        }
        for future in as_completed(futures):
            name, count = future.result()
            total_matches += count
            log.info(f"{name}: {count} matches")

    log.info(f"Done. {total_matches} total matches across {len(profiles)} profiles (run_id={run_id})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=50, help="Max matches per profile (masterprompt cap)")
    parser.add_argument("--days", type=int, default=10, help="Only consider jobs captured in the last N days")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent profiles processed in parallel")
    args = parser.parse_args()
    main(top_n=args.top, days=args.days, workers=args.workers)

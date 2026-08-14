"""
Run the resume-to-job matcher across all active base-resume profiles.
Report-only: writes to local resume_job_matches, never to Talentos.

Parallelism model
-----------------
Work is flattened to (profile, job-batch) units before dispatch, so
concurrency is NOT capped by the number of profiles. 19 profiles x ~10
batches each = ~190 independent LLM calls, which can run 100-wide.

Each completed batch is persisted immediately, so a timeout or crash never
loses finished work — re-run with --skip-done to continue.

Run: python -m scripts.match_resumes_to_jobs --workers 100
"""
import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

from app import db
from app.agents.matcher_agent import prefilter, score_batch, BATCH_SIZE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("match_resumes")


def load_profiles(include_test: bool = False, only_ready: bool = True) -> list[dict]:
    query = "SELECT * FROM resume_profiles WHERE 1=1"
    if not include_test:
        query += " AND is_test_account = 0"
    if only_ready:
        query += " AND is_match_ready = 1"
    query += " ORDER BY candidate_name, base_resume_name"
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(query).fetchall()]


def load_recent_jobs(days: int, posted_days: int | None = None) -> list[dict]:
    """
    days         how recently we captured the job (ingest recency)
    posted_days  how recently the EMPLOYER posted it (public freshness)

    posted_date is the one that matters to a candidate; scraped_at only says
    when we happened to see it. When posted_days is set, rows with an unknown
    posted_date are excluded rather than assumed fresh.
    """
    sql = """
        SELECT id, title, company_name, location, description, posted_date
        FROM keyword_jobs
        WHERE scraped_at >= datetime('now', ?)
    """
    params = [f"-{days} days"]
    if posted_days:
        sql += " AND posted_date IS NOT NULL AND date(posted_date) >= date('now', ?)"
        params.append(f"-{posted_days} days")
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def persist(profile_id: int, matches: list[dict], run_id: str) -> int:
    if not matches:
        return 0
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
                    profile_id, m["job_id"], m["score"], m["band"], m["reason"],
                    json.dumps(m["matched_terms"]), run_id,
                ),
            )
    return len(matches)


def trim_to_top_n(profile_id: int, top_n: int):
    """Enforce the per-profile cap after all batches land (masterprompt rule 11)."""
    with db.get_conn() as conn:
        conn.execute(
            """
            DELETE FROM resume_job_matches
            WHERE resume_profile_id = ?
              AND id NOT IN (
                SELECT id FROM resume_job_matches
                WHERE resume_profile_id = ?
                ORDER BY score DESC, id ASC
                LIMIT ?
              )
            """,
            (profile_id, profile_id, top_n),
        )


def collapse_within_candidate() -> int:
    """
    One row per (candidate, job), keeping the best-scoring resume file.

    Matching runs per resume FILE, so a candidate with three files can match
    the same job three times. applications has a unique index on
    (candidate_id, job_id) — the push collapses these to one application
    anyway — but leaving them in the local table inflates every count shown
    to an operator and, worse, lets a job filtered out of one file survive
    under another. An audit found 25 such rows, including senior-titled ones
    that a gate had already rejected elsewhere.
    """
    with db.get_conn() as conn:
        cur = conn.execute(
            """
            DELETE FROM resume_job_matches
            WHERE id NOT IN (
                SELECT keep_id FROM (
                    SELECT m.id AS keep_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY p.candidate_id, m.keyword_job_id
                               ORDER BY m.score DESC, m.id ASC
                           ) AS rn
                    FROM resume_job_matches m
                    JOIN resume_profiles p ON p.id = m.resume_profile_id
                ) WHERE rn = 1
            )
            """
        )
        return cur.rowcount


def resolve_cross_candidate_contention() -> int:
    """
    One candidate per job, decided here rather than left in the shortlist.

    Several candidates share a discipline — the CAD/drafting cluster all draft
    with AutoCAD/Civil 3D — so a job that fits one tends to fit all of them.
    An audit of a single night found 67 jobs claimed by 2-3 candidates each
    ("AutoCad Drafter @ MANTECH" wanted by three). Sending the same opening for
    several people through one agency weakens every submission, and
    scripts/deduplicate_candidate_jobs.py already enforces this — but only
    AFTER the applications exist in Talentos. Doing it here means the operator
    never sees the same job offered three times in Review & Assign.

    Winner is highest score, ties broken toward whoever is carrying fewer
    matches so load stays spread — the same order deduplicate_candidate_jobs
    uses once rows are live.
    """
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT m.id match_id, m.score, m.keyword_job_id,
                   p.candidate_id, p.candidate_name
            FROM resume_job_matches m
            JOIN resume_profiles p ON p.id = m.resume_profile_id
        """).fetchall()]

    by_job = defaultdict(list)
    for r in rows:
        by_job[r["keyword_job_id"]].append(r)

    load = defaultdict(int)
    for r in rows:
        load[r["candidate_id"]] += 1

    losers = []
    for _job_id, claims in by_job.items():
        if len(claims) < 2:
            continue
        winner = sorted(claims, key=lambda c: (-c["score"], load[c["candidate_id"]]))[0]
        for c in claims:
            if c["match_id"] != winner["match_id"]:
                losers.append(c["match_id"])
                load[c["candidate_id"]] -= 1

    if losers:
        with db.get_conn() as conn:
            conn.executemany("DELETE FROM resume_job_matches WHERE id=?",
                             [(m,) for m in losers])
    return len(losers)


def main(top_n: int, days: int, workers: int, include_test: bool, skip_done: bool,
         pool_size: int, posted_days: int | None = None):
    profiles = load_profiles(include_test=include_test)

    if skip_done:
        with db.get_conn() as conn:
            done = {r[0] for r in conn.execute(
                "SELECT DISTINCT resume_profile_id FROM resume_job_matches"
            ).fetchall()}
        before = len(profiles)
        profiles = [p for p in profiles if p["id"] not in done]
        log.info(f"--skip-done: skipping {before - len(profiles)} already-matched profiles")

    if not profiles:
        log.info("Nothing to do.")
        return

    jobs = load_recent_jobs(days, posted_days=posted_days)
    run_id = f"run_{int(time.time())}"
    freshness = f", posted within {posted_days}d" if posted_days else ""
    log.info(f"{len(profiles)} profiles x {len(jobs)} jobs{freshness} | run_id={run_id}")

    # Flatten to (profile, batch) work units so concurrency isn't profile-bound
    units = []
    for p in profiles:
        candidates = prefilter(p, jobs)[:pool_size]
        for i in range(0, len(candidates), BATCH_SIZE):
            units.append((p, candidates[i : i + BATCH_SIZE]))

    log.info(f"Dispatching {len(units)} batches across {workers} workers")

    done_counts = defaultdict(int)
    completed = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(score_batch, p, batch): p for p, batch in units}
        for future in as_completed(futures):
            profile = futures[future]
            try:
                matches = future.result()
            except Exception as e:
                log.warning(f"batch failed for {profile['base_resume_name']}: {e}")
                matches = []
            done_counts[profile["id"]] += persist(profile["id"], matches, run_id)
            completed += 1
            if completed % 25 == 0:
                rate = completed / max(time.time() - t0, 1)
                log.info(f"  {completed}/{len(units)} batches | {rate:.1f}/s")

    for p in profiles:
        trim_to_top_n(p["id"], top_n)
        log.info(f"{p['candidate_name']} / {p['base_resume_name']}: {min(done_counts[p['id']], top_n)} kept")

    collapsed = collapse_within_candidate()
    if collapsed:
        log.info(f"collapsed {collapsed} duplicate rows where one candidate matched "
                 f"the same job under more than one of their own resume files")

    contested = resolve_cross_candidate_contention()
    if contested:
        log.info(f"resolved {contested} cross-candidate claims — one candidate per job")

    elapsed = time.time() - t0
    log.info(f"Done in {elapsed:.0f}s. {len(units)} batches, run_id={run_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=50, help="Max matches kept per profile")
    parser.add_argument("--days", type=int, default=10, help="Only jobs captured in the last N days")
    parser.add_argument("--workers", type=int, default=100, help="Parallel LLM batch workers")
    parser.add_argument("--pool-size", type=int, default=250, help="Jobs per profile after prefilter")
    parser.add_argument("--include-test", action="store_true")
    parser.add_argument("--skip-done", action="store_true")
    parser.add_argument("--posted-days", type=int, default=None,
                        help="Only jobs the employer posted within N days (excludes unknown dates)")
    args = parser.parse_args()
    main(
        top_n=args.top, days=args.days, workers=args.workers,
        include_test=args.include_test, skip_done=args.skip_done,
        pool_size=args.pool_size, posted_days=args.posted_days,
    )

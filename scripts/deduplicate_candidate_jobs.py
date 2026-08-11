"""
Enforce one-candidate-per-job across the Talentos applications this tool created.

Why
---
Several candidates share a discipline (the CAD/drafting cluster: Bhaskar,
Avirup, Saddam, Maahir all draft with AutoCAD/Civil 3D). A job that fits one
tends to fit all of them, so the first push sent 19 jobs to 2+ candidates and
one job to 4. That is four people competing for one opening through the same
agency, which weakens every submission.

Allocation rule — the winner is the single best-fit candidate:
  1. highest match score
  2. tie-break: whoever has FEWER applications allocated so far, so a strong
     generalist does not absorb the whole queue
  3. final tie-break: stable ordering by application id

Only queued, unprocessed applications created by this tool are considered.
Anything an AE has already touched (ae_stage moved on, applied_at set, workflow
running/completed) is left alone — the point is to clean up my own duplicates,
not to reverse human work.

Run:  python -m scripts.deduplicate_candidate_jobs            (dry run)
      python -m scripts.deduplicate_candidate_jobs --commit
"""
import argparse
import logging
from collections import defaultdict

import psycopg

from app.config import NEON_DB_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("dedupe")

SOURCE_LABEL = "jobsearch_support"

# Every application competing for a contested job, including ones the pipeline
# has already advanced. Progress is part of the ranking: if the AI pipeline has
# already produced a tailored resume for one candidate, throwing that away to
# favour a marginally higher score wastes real work.
FETCH = """
SELECT a.id AS app_id, a.candidate_id, a.job_id, c.name AS candidate,
       j.title, j.company, a.source, a.ae_stage, a.applied_at,
       a.proof_url, w.match_score, w.status AS wf_status
FROM applications a
JOIN candidates c ON c.id = a.candidate_id
JOIN jobs j       ON j.id = a.job_id
LEFT JOIN application_ai_workflows w ON w.application_id = a.id
WHERE a.job_id IN (
    SELECT job_id FROM applications
    WHERE source = %s AND job_id IS NOT NULL
    GROUP BY job_id HAVING count(DISTINCT candidate_id) > 1
)
ORDER BY a.job_id, a.id
"""

# How far along an application is; higher wins a contest.
PROGRESS = {"in_ai_pipeline": 0, "ready_for_review": 1, "ready_for_application": 2, "applied": 3}


def is_removable(r: dict) -> bool:
    """Only ever remove our own untouched, still-queued rows."""
    return (
        r["source"] == SOURCE_LABEL
        and r["ae_stage"] == "in_ai_pipeline"
        and r["applied_at"] is None
        and r["proof_url"] is None
        and (r["wf_status"] or "queued") == "queued"
    )


def main(commit: bool):
    with psycopg.connect(NEON_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(FETCH, (SOURCE_LABEL,))
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            # current allocation load per candidate, for the tie-break
            cur.execute(
                "SELECT candidate_id, count(*) FROM applications WHERE source=%s GROUP BY 1",
                (SOURCE_LABEL,),
            )
            load = {c: n for c, n in cur.fetchall()}

            by_job = defaultdict(list)
            for r in rows:
                by_job[r["job_id"]].append(r)

            winners, losers = [], []
            for job_id, contenders in by_job.items():
                if len(contenders) < 2:
                    continue
                ranked = sorted(
                    contenders,
                    key=lambda r: (
                        -PROGRESS.get(r["ae_stage"], 0),      # keep the furthest along
                        -(float(r["match_score"]) if r["match_score"] is not None else 0),
                        load.get(r["candidate_id"], 0),
                        str(r["app_id"]),
                    ),
                )
                win = ranked[0]
                # never remove someone else's work, or anything already advanced
                lose = [r for r in ranked[1:] if is_removable(r)]
                if not lose:
                    continue
                winners.append(win)
                losers.extend(lose)
                # winner now carries one more; losers give one back
                for r in lose:
                    load[r["candidate_id"]] = max(0, load.get(r["candidate_id"], 1) - 1)

            log.info(f"contested jobs: {len(by_job)} | keep {len(winners)} | remove {len(losers)}")
            for w in winners[:10]:
                same = [x for x in by_job[w["job_id"]] if x["app_id"] != w["app_id"]]
                log.info(
                    f"  {w['title'][:38]:<38} -> KEEP {w['candidate'][:18]:<18} "
                    f"({w['match_score']}) over {', '.join(x['candidate'].split()[0] for x in same)}"
                )

            if not commit:
                log.info("DRY RUN — nothing removed. Re-run with --commit.")
                return

            removed = defaultdict(int)
            for r in losers:
                # children first; application_events/workflows cascade on delete,
                # but target_jobs is keyed on (candidate_id, job_id) and does not.
                cur.execute(
                    "DELETE FROM target_jobs WHERE candidate_id=%s AND job_id=%s",
                    (r["candidate_id"], r["job_id"]),
                )
                removed["target_jobs"] += cur.rowcount
                cur.execute("DELETE FROM applications WHERE id=%s", (r["app_id"],))
                removed["applications"] += cur.rowcount
            conn.commit()
            log.info("--- COMMITTED ---")
            for k, v in removed.items():
                log.info(f"  removed {k}: {v}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    main(ap.parse_args().commit)

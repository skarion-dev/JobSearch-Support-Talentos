"""
Repair AI workflows that were queued without a config_snapshot.

WHAT WENT WRONG
---------------
push_to_talentos inserted application_ai_workflows rows directly with
config_snapshot NULL. Talentos' own service builds that snapshot before
queueing, and the resume generator reads the candidate's resume OUT of it:

    config_snapshot = {
        job:            the job row
        baseResume:     a SEEDED application_resume_versions row carrying the
                        base resume's full content (header, summary, skills,
                        experience, education, projects, certifications)
        candidateId:    ...
        sourceOfTruth:  {notesContext, confirmedSkills}
        verifiedSkills: [...]
        evidence:       [...]
    }

With the snapshot NULL the generator had no resume to tailor, so it emitted a
header-and-skills skeleton with experience: [] and education: [] — and an ATS
score of 0. Measured: 367 of our 431 applications scored 0, against 0 zeros
across 647 of Talentos' own.

This was NOT caused by short job descriptions. Talentos' own target_jobs
average 1,477 characters against our 2,730 and still score 7.49.

WHAT THIS DOES
--------------
For each affected application: seeds the application_resume_versions row the
same way the service does, rebuilds config_snapshot from live data, and resets
the workflow to queued so the dispatcher regenerates the resume properly.

Dry run by default.

    python -m scripts.repair_ai_workflows --limit 3
    python -m scripts.repair_ai_workflows --limit 3 --commit
"""
import argparse
import json
import logging

import psycopg
from psycopg.types.json import Jsonb

from app.config import NEON_DB_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("repair")

SOURCE_LABEL = "jobsearch_support"

# Only the job fields Talentos puts in the snapshot
JOB_FIELDS = ("id", "notes", "title", "ref_id", "source", "company", "benefits",
              "location", "apply_url", "input_url", "is_active", "posted_at")

FETCH = f"""
SELECT a.id app_id, a.candidate_id, a.base_resume_id, a.job_id,
       w.id workflow_id, v.id existing_version_id,
       b.content base_content, b.name base_title,
       t.id target_job_id,
       row_to_json(j) job_row,
       c.verified_skills,
       s.confirmed_skills, s.notes
FROM applications a
JOIN application_ai_workflows w ON w.application_id = a.id
JOIN jobs j        ON j.id = a.job_id
JOIN candidates c  ON c.id = a.candidate_id
JOIN base_resumes b ON b.id = a.base_resume_id
LEFT JOIN target_jobs t ON t.candidate_id = a.candidate_id AND t.job_id = a.job_id
LEFT JOIN application_resume_versions v ON v.id = a.tailored_resume_version_id
LEFT JOIN candidate_source_of_truth s ON s.candidate_id = a.candidate_id
WHERE a.source = %s
  AND w.config_snapshot IS NULL
  AND a.applied_at IS NULL          -- never touch anything already applied
ORDER BY a.created_at
"""


def build_snapshot(row: dict, seed_version: dict) -> dict:
    job = {k: v for k, v in (row["job_row"] or {}).items() if k in JOB_FIELDS}
    return {
        "job": job,
        "baseResume": seed_version,
        "candidateId": str(row["candidate_id"]),
        "sourceOfTruth": {
            "notesContext": row.get("notes") or "",
            "confirmedSkills": row.get("confirmed_skills") or [],
        },
        "verifiedSkills": row.get("verified_skills") or [],
        "evidence": [],
    }


def main(limit: int | None, commit: bool):
    with psycopg.connect(NEON_DB_URL) as conn, conn.cursor() as cur:
        cur.execute(FETCH, (SOURCE_LABEL,))
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if limit:
            rows = rows[:limit]

        log.info(f"{len(rows)} applications have a workflow with no config_snapshot")
        missing_content = [r for r in rows if not r["base_content"]]
        if missing_content:
            log.warning(f"{len(missing_content)} have no base resume content and will be skipped")
        rows = [r for r in rows if r["base_content"]]

        if not commit:
            for r in rows[:5]:
                sections = {k: (len(v) if isinstance(v, list) else "obj")
                            for k, v in (r["base_content"] or {}).items()}
                log.info(f"  would repair app {str(r['app_id'])[:8]} "
                         f"base='{r['base_title']}' sections={sections}")
            log.info("DRY RUN - nothing written. Re-run with --commit.")
            return

        repaired = 0
        for r in rows:
            # 1. Seed the resume version the way the service does, carrying the
            #    base resume content so the generator has something to tailor.
            cur.execute(
                """
                INSERT INTO application_resume_versions
                    (candidate_id, base_resume_id, target_job_id, content, formatting,
                     status, source_type, created_by)
                VALUES (%s,%s,%s,%s,%s,'active','base_resume',%s)
                RETURNING id
                """,
                (r["candidate_id"], r["base_resume_id"], r["target_job_id"],
                 Jsonb(r["base_content"]),
                 Jsonb((r["base_content"] or {}).get("formatting") or {}),
                 None),
            )
            seed_id = cur.fetchone()[0]

            cur.execute("SELECT row_to_json(v) FROM application_resume_versions v WHERE v.id=%s",
                        (seed_id,))
            seed_version = cur.fetchone()[0]

            snapshot = build_snapshot(r, seed_version)

            # 2. Point the application at the seeded version and reset generation
            cur.execute(
                """UPDATE applications
                   SET tailored_resume_version_id=%s, resume_generation_status='queued',
                       resume_generation_error=NULL
                   WHERE id=%s""",
                (seed_id, r["app_id"]),
            )

            # 3. Requeue the workflow WITH the snapshot so the generator can read it
            cur.execute(
                """UPDATE application_ai_workflows
                   SET config_snapshot=%s, status='queued', current_stage=0,
                       last_error=NULL, completed_at=NULL, claimed_at=NULL,
                       claim_expires_at=NULL, recovery_count=0
                   WHERE id=%s""",
                (Jsonb(snapshot), r["workflow_id"]),
            )
            repaired += 1

        conn.commit()
        log.info(f"Repaired {repaired} workflows - they will regenerate with the resume content.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    main(a.limit, a.commit)

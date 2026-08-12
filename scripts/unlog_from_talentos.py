"""
Remove every application this tool logged to Talentos, so the push can be redone.

Deletes ONLY rows created by this tool (source='jobsearch_support') and ONLY
when untouched by a human: no applied_at, no proof, no AE review, and the stage
still where the AI pipeline left it. Anything a person has acted on is left
alone and reported.

Delete order matters:
    applications          (events + ai_workflows CASCADE from here)
    application_resume_versions   referenced by applications and target_jobs
    target_jobs           keyed on (candidate_id, job_id), no cascade

Job rows are kept by default — they are catalogue entries a future push will
dedupe against. --drop-thin-jobs additionally removes jobs this tool created
that still have an unusable description and that nothing else references.

Dry run by default.
"""
import argparse
import logging

import psycopg

from app.config import NEON_DB_URL
from app.quality import MIN_DESCRIPTION as MIN_GOOD

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("unlog")

SOURCE_LABEL = "jobsearch_support"

UNTOUCHED = """
    source = %s
    AND applied_at IS NULL
    AND proof_url IS NULL
    AND ae_reviewed_by_user_id IS NULL
    AND ae_applied_by_user_id IS NULL
    AND ae_stage IN ('in_ai_pipeline','ready_for_review')
"""


def main(commit: bool, drop_thin_jobs: bool):
    with psycopg.connect(NEON_DB_URL) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM applications WHERE {UNTOUCHED}", (SOURCE_LABEL,))
        removable = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM applications WHERE source=%s", (SOURCE_LABEL,))
        total = cur.fetchone()[0]
        log.info(f"{total} applications from this tool; {removable} safe to remove, "
                 f"{total - removable} human-touched and will be KEPT")

        cur.execute(f"""
            SELECT count(*) FROM target_jobs t WHERE EXISTS (
              SELECT 1 FROM applications a WHERE {UNTOUCHED}
                AND a.candidate_id=t.candidate_id AND a.job_id=t.job_id)
        """, (SOURCE_LABEL,))
        tj = cur.fetchone()[0]

        cur.execute("SELECT count(*) FROM jobs WHERE updated_by=%s", (SOURCE_LABEL,))
        our_jobs = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*) FROM jobs j WHERE j.updated_by=%s
              AND length(coalesce(j.description_text,j.raw_description,'')) < %s
              AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=j.id)
        """, (SOURCE_LABEL, MIN_GOOD))
        thin_jobs = cur.fetchone()[0]

        log.info(f"cascade: {tj} target_jobs, plus events and ai_workflows")
        log.info(f"jobs created by this tool: {our_jobs} ({thin_jobs} thin and unreferenced)")

        if not commit:
            log.info("DRY RUN - nothing deleted. Re-run with --commit.")
            return

        # resume versions first: applications point at them, and they point at target_jobs
        cur.execute(f"""
            DELETE FROM application_resume_versions v
            WHERE EXISTS (
              SELECT 1 FROM applications a
              WHERE {UNTOUCHED} AND (a.tailored_resume_version_id = v.id
                 OR (v.candidate_id = a.candidate_id AND v.base_resume_id = a.base_resume_id
                     AND v.created_by IS NULL))
            )
        """, (SOURCE_LABEL,))
        versions = cur.rowcount

        cur.execute(f"""
            DELETE FROM target_jobs t WHERE EXISTS (
              SELECT 1 FROM applications a WHERE {UNTOUCHED}
                AND a.candidate_id=t.candidate_id AND a.job_id=t.job_id)
        """, (SOURCE_LABEL,))
        targets = cur.rowcount

        cur.execute(f"DELETE FROM applications WHERE {UNTOUCHED}", (SOURCE_LABEL,))
        apps = cur.rowcount

        dropped = 0
        if drop_thin_jobs:
            cur.execute("""
                DELETE FROM jobs j WHERE j.updated_by=%s
                  AND length(coalesce(j.description_text,j.raw_description,'')) < %s
                  AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=j.id)
                  AND NOT EXISTS (SELECT 1 FROM target_jobs t WHERE t.job_id=j.id)
            """, (SOURCE_LABEL, MIN_GOOD))
            dropped = cur.rowcount

        conn.commit()
        log.info(f"removed: {apps} applications (events + workflows cascaded), "
                 f"{targets} target_jobs, {versions} resume versions, {dropped} thin jobs")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--drop-thin-jobs", action="store_true",
                    help="also delete unusable job rows this tool created")
    a = ap.parse_args()
    main(a.commit, a.drop_thin_jobs)

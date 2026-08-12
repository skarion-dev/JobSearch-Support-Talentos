"""
Build the AI-workflow payload Talentos' own service builds.

WHY THIS EXISTS
---------------
The first push inserted application_ai_workflows rows directly with
config_snapshot NULL. That is where the resume generator reads the candidate's
resume from, so with it missing the generator produced a header-and-skills
skeleton: experience [], education [], ATS score 0. 367 of 431 applications
scored 0, against 0 zeros across 647 of Talentos' own.

The cause was NOT short job descriptions. Talentos' own target_jobs average
1,477 characters against our 2,730 and still score 7.49.

The real contract, reverse-engineered from 696 working workflows:

    application_resume_versions   a SEEDED row carrying the base resume's full
                                  content, created BEFORE the workflow
    applications.tailored_resume_version_id -> that row
    application_ai_workflows.config_snapshot = {
        job, baseResume, candidateId, sourceOfTruth, verifiedSkills, evidence
    }

Everything here is derived from live Talentos data. Nothing is invented.
"""
import logging

from psycopg.types.json import Jsonb

log = logging.getLogger("talentos_workflow")

# The job fields Talentos includes in the snapshot, and only those.
JOB_FIELDS = ("id", "notes", "title", "ref_id", "source", "company", "benefits",
              "location", "apply_url", "input_url", "is_active", "posted_at")


def fetch_candidate_context(cur, candidate_id) -> dict:
    """verified skills and source-of-truth notes, as the service supplies them."""
    cur.execute(
        "SELECT coalesce(verified_skills, '{}') FROM candidates WHERE id = %s",
        (candidate_id,),
    )
    row = cur.fetchone()
    verified = list(row[0]) if row and row[0] else []

    cur.execute(
        """SELECT coalesce(confirmed_skills, '{}'), coalesce(notes, '')
           FROM candidate_source_of_truth WHERE candidate_id = %s""",
        (candidate_id,),
    )
    sot = cur.fetchone()
    confirmed = list(sot[0]) if sot and sot[0] else []
    notes = sot[1] if sot else ""

    return {"verified_skills": verified, "confirmed_skills": confirmed, "notes": notes}


def seed_resume_version(cur, candidate_id, base_resume_id, target_job_id, actor_id=None):
    """
    Create the resume version the generator will tailor, pre-filled with the
    base resume's content. Returns (version_id, version_row) or (None, None)
    when the base resume has no content to seed from.
    """
    cur.execute("SELECT content FROM base_resumes WHERE id = %s", (base_resume_id,))
    row = cur.fetchone()
    content = row[0] if row else None
    if not content:
        return None, None

    cur.execute(
        """
        INSERT INTO application_resume_versions
            (candidate_id, base_resume_id, target_job_id, content, formatting,
             status, source_type, created_by)
        VALUES (%s,%s,%s,%s,%s,'active','base_resume',%s)
        RETURNING id
        """,
        (candidate_id, base_resume_id, target_job_id,
         Jsonb(content), Jsonb(content.get("formatting") or {}), actor_id),
    )
    version_id = cur.fetchone()[0]

    cur.execute("SELECT row_to_json(v) FROM application_resume_versions v WHERE v.id = %s",
                (version_id,))
    return version_id, cur.fetchone()[0]


def build_config_snapshot(cur, candidate_id, job_id, seed_version: dict) -> dict:
    cur.execute("SELECT row_to_json(j) FROM jobs j WHERE j.id = %s", (job_id,))
    job_row = cur.fetchone()[0] or {}
    ctx = fetch_candidate_context(cur, candidate_id)

    return {
        "job": {k: v for k, v in job_row.items() if k in JOB_FIELDS},
        "baseResume": seed_version,
        "candidateId": str(candidate_id),
        "sourceOfTruth": {
            "notesContext": ctx["notes"],
            "confirmedSkills": ctx["confirmed_skills"],
        },
        "verifiedSkills": ctx["verified_skills"],
        "evidence": [],
    }


def prepare_workflow_payload(cur, candidate_id, base_resume_id, job_id,
                             target_job_id, actor_id=None):
    """
    Seed the resume version and build the snapshot.

    Returns (version_id, config_snapshot) or (None, None) if the base resume
    has no content — in that case the caller should NOT queue a workflow,
    because it would regenerate the same empty resume.
    """
    version_id, version_row = seed_resume_version(
        cur, candidate_id, base_resume_id, target_job_id, actor_id
    )
    if not version_id:
        log.warning(f"base resume {base_resume_id} has no content; skipping workflow")
        return None, None

    return version_id, build_config_snapshot(cur, candidate_id, job_id, version_row)

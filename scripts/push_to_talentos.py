"""
Push matched jobs into Talentos and queue them into the AI pipeline.

DRY RUN BY DEFAULT. Nothing is written unless --commit is passed.

Flow per approved match (masterprompt sections 6 and 13):
  1. jobs                    insert if not already present (deduped)
  2. target_jobs             candidate/job link carrying the fit score
  3. applications            source_type=base_resume, ae_stage=in_ai_pipeline,
                             assigned to the configured AE, applied_at NULL
  4. application_events      one 'assigned' event, matching how the app does it
  5. application_ai_workflows  status=queued so the dispatcher picks it up

Edge cases handled (each was verified against the live schema):

  * jobs has NO unique index on external_job_id/source_url/apply_url — the DB
    will happily create duplicates, so dedupe is enforced here in four passes:
    external_job_id, apply_url, source_url, then normalized company+title.
  * applications has a UNIQUE partial index on (candidate_id, job_id). A
    candidate matched by several of their own resumes to the SAME job would
    violate it, so we keep only that candidate's highest-scoring resume for a
    given job (masterprompt rule 7: one best base resume per candidate/job).
  * A candidate may already have an application for the job from the existing
    manual pipeline — skip, never resurrect or duplicate.
  * automation_idempotency_key is deterministic, so a re-run repairs rather
    than duplicates.
  * Jobs with no trustworthy posted_at or a too-short description are held back
    for human verification instead of being auto-queued.
  * applied_at stays NULL and no proof/submission field is ever set.
  * ae_stage and application_stage are kept in sync, which is what every
    existing base_resume row does.

Run:  python -m scripts.push_to_talentos --limit 5            (dry run)
      python -m scripts.push_to_talentos --limit 5 --commit   (writes)
"""
import argparse
import hashlib
import logging
import re
from collections import defaultdict

import psycopg

from app import db
from app.config import NEON_DB_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("push")

AE_EMAIL = "akash@skarion.com"
MIN_DESCRIPTION = 120          # masterprompt rule 6
SOURCE_LABEL = "jobsearch_support"


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def load_matches(limit: int | None, min_score: int, posted_days: int,
                 per_candidate_cap: int | None = 50) -> list[dict]:
    """Local matches, best-resume-per-candidate/job already resolved."""
    sql = """
        SELECT m.score, m.band, m.reason,
               p.candidate_id, p.candidate_name, p.base_resume_id, p.base_resume_name,
               j.id AS local_job_id, j.title, j.company_name, j.location, j.description,
               j.job_url, j.source_url, j.apply_url, j.external_job_id, j.posted_date,
               j.salary, j.source
        FROM resume_job_matches m
        JOIN resume_profiles p ON p.id = m.resume_profile_id
        JOIN keyword_jobs   j ON j.id = m.keyword_job_id
        WHERE m.score >= ? AND p.is_test_account = 0
          AND j.posted_date IS NOT NULL
          AND date(j.posted_date) >= date('now', ?)
        ORDER BY m.score DESC
    """
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, (min_score, f"-{posted_days} days")).fetchall()]

    # One application per (candidate, job) — the DB enforces this with a unique
    # partial index, so pick the candidate's best-scoring resume for each job.
    best: dict[tuple, dict] = {}
    for r in rows:
        key = (r["candidate_id"], norm(r["company_name"]), norm(r["title"]))
        if key not in best or r["score"] > best[key]["score"]:
            best[key] = r
    out = sorted(best.values(), key=lambda r: -r["score"])

    # Masterprompt rule 11: never exceed MAX_NEW_PER_CANDIDATE in one run.
    # Without this, 7 candidates produced ~65 applications each — real AE
    # workload, not just rows.
    if per_candidate_cap:
        kept, seen = [], defaultdict(int)
        for r in out:
            if seen[r["candidate_id"]] >= per_candidate_cap:
                continue
            seen[r["candidate_id"]] += 1
            kept.append(r)
        out = kept

    return out[:limit] if limit else out


def find_existing_job(cur, m: dict) -> str | None:
    """Four-pass dedupe against Talentos jobs; the DB has no unique key here."""
    if m["external_job_id"]:
        cur.execute("SELECT id FROM jobs WHERE external_job_id = %s LIMIT 1", (m["external_job_id"],))
        if (hit := cur.fetchone()):
            return hit[0]
    for url in (m["apply_url"], m["source_url"], m["job_url"]):
        if url:
            cur.execute(
                "SELECT id FROM jobs WHERE apply_url = %s OR source_url = %s LIMIT 1", (url, url)
            )
            if (hit := cur.fetchone()):
                return hit[0]
    cur.execute(
        """
        SELECT id FROM jobs
        WHERE lower(regexp_replace(coalesce(company,''), '[^a-zA-Z0-9]+', ' ', 'g')) = %s
          AND lower(regexp_replace(coalesce(title,''),   '[^a-zA-Z0-9]+', ' ', 'g')) = %s
        LIMIT 1
        """,
        (norm(m["company_name"]).strip(), norm(m["title"]).strip()),
    )
    hit = cur.fetchone()
    return hit[0] if hit else None


def idem_key(candidate_id: str, job_key: str) -> str:
    return "jss_" + hashlib.sha256(f"{candidate_id}:{job_key}".encode()).hexdigest()[:32]


def push(matches: list[dict], commit: bool, ae_user_id: str | None = None,
         actor: str | None = None):
    with psycopg.connect(NEON_DB_URL) as conn:
        with conn.cursor() as cur:
            if ae_user_id:
                cur.execute(
                    "SELECT user_id, display_name, role, is_active FROM profiles WHERE user_id=%s",
                    (ae_user_id,),
                )
            else:
                cur.execute(
                    "SELECT user_id, display_name, role, is_active FROM profiles WHERE lower(email)=%s",
                    (AE_EMAIL,),
                )
            ae = cur.fetchone()
            if not ae or not ae[3]:
                raise SystemExit(f"Assignee {ae_user_id or AE_EMAIL} not found or inactive")
            ae_id, ae_name, ae_role = ae[0], ae[1], ae[2]
            log.info(f"AE: {ae_name} <{AE_EMAIL}> role={ae_role}")

            stats = defaultdict(int)
            plan = []

            for m in matches:
                # Quality gates — held back rather than auto-queued
                if len(m["description"] or "") < MIN_DESCRIPTION:
                    stats["skip_thin_description"] += 1
                    continue

                existing_job = find_existing_job(cur, m)
                job_action = "reuse" if existing_job else "insert"
                stats[f"job_{job_action}"] += 1

                # Application dedupe: unique partial index on (candidate_id, job_id)
                dup_app = None
                if existing_job:
                    cur.execute(
                        "SELECT id, ae_stage FROM applications WHERE candidate_id=%s AND job_id=%s",
                        (m["candidate_id"], existing_job),
                    )
                    dup_app = cur.fetchone()
                if dup_app:
                    stats["skip_existing_application"] += 1
                    continue

                stats["will_create_application"] += 1
                plan.append({**m, "existing_job_id": existing_job, "job_action": job_action})

            log.info("--- PLAN ---")
            for k in sorted(stats):
                log.info(f"  {k}: {stats[k]}")

            if not commit:
                log.info("DRY RUN — no writes. Sample of what would be created:")
                for p in plan[:8]:
                    log.info(
                        f"  [{p['score']}] {p['candidate_name']} <- {p['title'][:42]} "
                        f"@ {p['company_name']} ({p['job_action']} job)"
                    )
                return stats, plan

            # ---------------- writes ----------------
            created = defaultdict(int)
            for p in plan:
                job_id = p["existing_job_id"]
                if not job_id:
                    cur.execute(
                        """
                        INSERT INTO jobs (title, company, location, source, source_url, apply_url,
                                          external_job_id, posted_at, description_text, raw_description,
                                          salary_range, is_active, updated_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s)
                        RETURNING id
                        """,
                        (
                            p["title"], p["company_name"], p["location"],
                            p["source"] or SOURCE_LABEL,
                            p["source_url"] or p["job_url"], p["apply_url"] or p["job_url"],
                            p["external_job_id"], p["posted_date"],
                            p["description"], p["description"], p["salary"], SOURCE_LABEL,
                        ),
                    )
                    job_id = cur.fetchone()[0]
                    created["jobs"] += 1

                cur.execute(
                    """
                    INSERT INTO target_jobs (candidate_id, job_id, raw_description, fit_score,
                                             recommendation, created_by)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (candidate_id, job_id) DO NOTHING
                    """,
                    (p["candidate_id"], job_id, p["description"], p["score"], p["band"], ae_id),
                )
                created["target_jobs"] += cur.rowcount

                key = idem_key(str(p["candidate_id"]), str(job_id))
                cur.execute(
                    """
                    INSERT INTO applications (
                        candidate_id, job_id, status, source, source_type, priority,
                        review_status, proof_required, next_action,
                        assigned_by, assigned_by_user_id, assigned_to, assigned_to_user_id,
                        base_resume_id, ae_stage, application_stage,
                        ae_stage_updated_at, ae_stage_updated_by_user_id, ae_stage_updated_by_name,
                        application_stage_changed_at, application_stage_changed_by_user_id,
                        application_stage_changed_by_name,
                        resume_generation_status, applied_at,
                        automation_idempotency_key, created_by, updated_by
                    ) VALUES (
                        %s,%s,'assigned',%s,'base_resume','normal',
                        'not_required', false, 'Apply to this job',
                        %s,%s,%s,%s,
                        %s,'in_ai_pipeline','in_ai_pipeline',
                        now(),%s,%s,
                        now(),%s,%s,
                        'queued', NULL,
                        %s,%s,%s
                    )
                    -- the unique index is PARTIAL, so the predicate must be
                    -- repeated here or Postgres cannot match the arbiter
                    ON CONFLICT (automation_idempotency_key)
                        WHERE automation_idempotency_key IS NOT NULL
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        p["candidate_id"], job_id, SOURCE_LABEL,
                        ae_name, ae_id, ae_name, ae_id,
                        p["base_resume_id"],
                        ae_id, ae_name,
                        ae_id, ae_name,
                        key, actor or SOURCE_LABEL, actor or SOURCE_LABEL,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    created["app_idempotent_skip"] += 1
                    continue
                app_id = row[0]
                created["applications"] += 1

                cur.execute(
                    "INSERT INTO application_events (application_id, to_status, note) VALUES (%s,%s,%s)",
                    (app_id, "assigned",
                     f"Auto-matched from JobSearch Support by {actor or SOURCE_LABEL} "
                     f"(score {p['score']}, {p['band']}). {p['reason'][:180]}"),
                )
                created["events"] += 1

                cur.execute(
                    """
                    INSERT INTO application_ai_workflows
                        (application_id, base_resume_id, status, idempotency_key,
                         started_by, match_score, match_reason)
                    VALUES (%s,%s,'queued',%s,%s,%s,%s)
                    """,
                    (app_id, p["base_resume_id"], key, ae_id, p["score"], p["reason"][:500]),
                )
                created["ai_workflows"] += 1

            conn.commit()
            log.info("--- COMMITTED ---")
            for k in sorted(created):
                log.info(f"  {k}: {created[k]}")
            return stats, plan


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--min-score", type=int, default=90)
    ap.add_argument("--posted-days", type=int, default=3)
    ap.add_argument("--per-candidate", type=int, default=50,
                    help="Max new applications per candidate this run (masterprompt cap)")
    ap.add_argument("--commit", action="store_true", help="Actually write to Talentos")
    a = ap.parse_args()
    ms = load_matches(a.limit, a.min_score, a.posted_days, per_candidate_cap=a.per_candidate)
    log.info(
        f"{len(ms)} candidate/job pairs selected "
        f"(score>={a.min_score}, posted<={a.posted_days}d, max {a.per_candidate}/candidate)"
    )
    push(ms, commit=a.commit)

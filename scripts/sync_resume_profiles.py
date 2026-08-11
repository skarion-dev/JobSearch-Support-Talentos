"""
One-time/refreshable sync: pull active candidates, their base resumes, and
resume search profiles from the Talentos Neon DB (READ-ONLY — never writes
back). Only fields needed for job matching are pulled (masterprompt rule 5):
resume content, verified skills, work authorization/visa, location
preference, and profile rules/keywords. No email, notes, EEO fields, or
other private data is stored.

Stored in local SQLite only (data/jobsearch.db, gitignored) — never
committed to GitHub, never written back to Neon.

Run: python -m scripts.sync_resume_profiles
"""
import json
import psycopg
from app import db
from app.config import NEON_DB_URL

CANDIDATES_QUERY = """
SELECT id, name, status, target_roles, preferred_locations, work_authorization,
       visa_status, verified_skills, location_preference, open_to_relocation
FROM candidates
WHERE lower(status) = 'active'
"""

BASE_RESUMES_QUERY = """
SELECT id, candidate_id, name, target_industry, target_roles, status, content
FROM base_resumes
WHERE candidate_id = ANY(%s)
"""

PROFILES_QUERY = """
SELECT id, candidate_id, base_resume_id, keywords, additional_rules,
       keyword_states, generation_status, review_status,
       resume_content_hash, approved_profile_version, profile_version, rules_json
FROM candidate_resume_search_profiles
WHERE base_resume_id = ANY(%s) AND disabled_at IS NULL
"""


def main():
    if not NEON_DB_URL:
        raise SystemExit("NEON_DB_URL not set in .env")

    with psycopg.connect(NEON_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(CANDIDATES_QUERY)
            cols = [d.name for d in cur.description]
            candidates = [dict(zip(cols, row)) for row in cur.fetchall()]
            candidate_ids = [c["id"] for c in candidates]

            cur.execute(BASE_RESUMES_QUERY, (candidate_ids,))
            cols = [d.name for d in cur.description]
            resumes = [dict(zip(cols, row)) for row in cur.fetchall()]
            resume_ids = [r["id"] for r in resumes]

            cur.execute(PROFILES_QUERY, (resume_ids,))
            cols = [d.name for d in cur.description]
            profiles = [dict(zip(cols, row)) for row in cur.fetchall()]

    print(f"Fetched {len(candidates)} active candidates, {len(resumes)} base resumes, {len(profiles)} profiles")

    candidates_by_id = {c["id"]: c for c in candidates}
    resumes_by_id = {r["id"]: r for r in resumes}

    with db.get_conn() as conn:
        conn.execute("DELETE FROM resume_profiles")
        for p in profiles:
            candidate = candidates_by_id.get(p["candidate_id"])
            resume = resumes_by_id.get(p["base_resume_id"])
            if not candidate or not resume:
                continue

            keyword_states = p.get("keyword_states") or []
            if keyword_states:
                active_keywords = [
                    ks["term"] for ks in keyword_states
                    if ks.get("status") not in ("dismissed", "rejected")
                ]
            else:
                active_keywords = list(p.get("keywords") or [])

            # Only auto-match-ready per masterprompt rule 4
            ready = (
                p.get("review_status") == "approved"
                and p.get("approved_profile_version") == p.get("profile_version")
            )

            conn.execute(
                """
                INSERT INTO resume_profiles
                    (candidate_id, candidate_name, base_resume_id, base_resume_name,
                     target_roles, work_authorization, visa_status, verified_skills,
                     location_preference, open_to_relocation, keywords, additional_rules,
                     review_status, generation_status, is_match_ready)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(candidate["id"]),
                    candidate["name"],
                    str(resume["id"]),
                    resume["name"],
                    json.dumps(list(resume.get("target_roles") or [])),
                    candidate.get("work_authorization"),
                    candidate.get("visa_status"),
                    json.dumps(list(candidate.get("verified_skills") or [])),
                    candidate.get("location_preference") or candidate.get("preferred_locations"),
                    1 if candidate.get("open_to_relocation") else 0,
                    json.dumps(active_keywords),
                    p.get("additional_rules"),
                    p.get("review_status"),
                    p.get("generation_status"),
                    1 if ready else 0,
                ),
            )

    print("Synced to local resume_profiles table (private, gitignored).")


if __name__ == "__main__":
    main()

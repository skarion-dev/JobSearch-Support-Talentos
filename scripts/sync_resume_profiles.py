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
from app.experience import education_bonus, years_of_experience

# Operator override, per candidate — for cases no resume field can encode.
# Najiur computes to 6.1 raw years (the most senior on the roster) and holds
# a Master's, so a formula would push him MORE senior, not less. Real-world
# placement judgment (2026-08-14): he is weaker in practice than his years
# suggest and should be steered toward roles wanting LESS experience. This
# overrides the computed years_of_experience()+education_bonus() entirely —
# it is not a formula input, it is operator knowledge the data can't show.
# Revisit/remove if that assessment changes.
EXPERIENCE_OVERRIDES = {
    "Mir Najiur Rahman": 2.5,
}

# Masterprompt section 5: test accounts are active in the snapshot but excluded
# from production runs unless the operator explicitly includes them.
TEST_ACCOUNTS = {"Test Istiaque", "akash"}

# Operator directive (2026-08-11): treat every base resume belonging to the six
# real active candidates as approved for MATCHING purposes, overriding the
# review_status/profile_version gate from masterprompt rule 4.
#
# This only affects local scoring and the report. It does NOT authorize any
# write to Talentos — the push path is still gated on a per-match human
# approval (see docs/PIPELINE_PLAN.md, Step 0).
OPERATOR_APPROVES_ALL_NON_TEST = True

# Per-candidate location constraints set by the operator. These are matching
# rules, not data from Neon, so they live here rather than being invented from
# candidate fields (Najiur's location columns are all NULL upstream).
# Machine-enforced gate key (app/filters.GATES) — the free-text rule below is
# only advisory context for the model; this is what actually filters.
LOCATION_GATES = {
    "Mir Najiur Rahman": "dmv_or_remote",
}

LOCATION_RULES = {
    "Mir Najiur Rahman": (
        "LOCATION HARD GATE: only accept jobs within a 100-mile radius of the DMV "
        "region (Washington DC, Maryland, Northern Virginia — including Baltimore, "
        "Richmond, Arlington, Alexandria, Bethesda, Rockville, Fairfax) OR jobs that "
        "are fully remote / remote-US. Reject anything outside that radius that is "
        "not remote."
    ),
}

# NOTE: candidates.status is 'active' for every row, including dropped and
# placed people — it does not gate anything. The real pipeline gate is
# candidates.pipeline_stage, which is what the Talentos UI shows in its STAGE
# column: applying (shown as "Active") | not_started | placed | dropped.
# Only 'applying' candidates should be matched.
ACTIVE_STAGE = "applying"

CANDIDATES_QUERY = """
SELECT id, name, status, pipeline_stage, target_roles, preferred_locations,
       work_authorization, visa_status, verified_skills, location_preference,
       open_to_relocation
FROM candidates
WHERE lower(pipeline_stage) = %s
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


def _effective_years(candidate_name: str, content: dict | None) -> float | None:
    """years_of_experience() + education_bonus(), unless the operator has
    explicitly overridden this candidate — see EXPERIENCE_OVERRIDES above."""
    if candidate_name in EXPERIENCE_OVERRIDES:
        return EXPERIENCE_OVERRIDES[candidate_name]
    raw = years_of_experience(content)
    if raw is None:
        return None
    return round(raw + education_bonus(content), 1)


def main():
    if not NEON_DB_URL:
        raise SystemExit("NEON_DB_URL not set in .env")

    with psycopg.connect(NEON_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(CANDIDATES_QUERY, (ACTIVE_STAGE,))
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

    # A base resume can exist with no candidate_resume_search_profiles row
    # (e.g. Mir Najiur Rahman's approved CAD resume). Requiring a profile row
    # silently dropped those candidates. Synthesize a minimal profile from the
    # resume itself so they still get matched.
    def keywords_from_resume(resume: dict) -> list[str]:
        """
        Derive search terms from the resume itself for profiles that have no
        generated keyword set. content.skills is a list of
        {title, skills: [...]} groups, which is exactly the vocabulary a
        keyword search needs.
        """
        terms: list[str] = list(resume.get("target_roles") or [])
        content = resume.get("content")
        if isinstance(content, dict):
            for group in content.get("skills") or []:
                if not isinstance(group, dict):
                    continue
                if group.get("title"):
                    terms.append(str(group["title"]))
                for s in group.get("skills") or []:
                    if s:
                        terms.append(str(s))
            for exp in content.get("experience") or []:
                if isinstance(exp, dict) and exp.get("title"):
                    terms.append(str(exp["title"]))
        seen, out = set(), []
        for t in terms:
            t = t.strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    def target_roles_from_resume(resume: dict) -> list[str]:
        """
        Talentos' own base_resumes.target_roles is empty for every resume in
        this roster — the matcher prompt asks the model "Target roles: []"
        every single time, a dead field worse than not asking at all. Resume
        content already lists experience most-recent-first (standard resume
        convention, same assumption keywords_from_resume relies on), so the
        candidate's own recent job titles are a real, free substitute.
        """
        provided = list(resume.get("target_roles") or [])
        if provided:
            return provided
        content = resume.get("content")
        if not isinstance(content, dict):
            return []
        titles = [
            str(e["title"]) for e in (content.get("experience") or [])
            if isinstance(e, dict) and e.get("title")
        ]
        return titles[:2]

    profiled_resume_ids = {p["base_resume_id"] for p in profiles}
    for r in resumes:
        if r["id"] in profiled_resume_ids:
            continue
        if (r.get("status") or "").lower() in ("archived", "disabled", "deleted"):
            continue
        profiles.append(
            {
                "candidate_id": r["candidate_id"],
                "base_resume_id": r["id"],
                "keywords": keywords_from_resume(r),
                "keyword_states": [],
                "additional_rules": None,
                "review_status": "no_profile_row",
                "generation_status": "synthesized_from_resume",
                "approved_profile_version": None,
                "profile_version": None,
            }
        )

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

            is_test_account = candidate["name"] in TEST_ACCOUNTS

            # Masterprompt rule 4 gate, unless the operator has overridden it.
            ready = (
                p.get("review_status") == "approved"
                and p.get("approved_profile_version") == p.get("profile_version")
            )
            if OPERATOR_APPROVES_ALL_NON_TEST and not is_test_account:
                ready = True

            # Operator-set location constraint, appended to any upstream rules
            rules = p.get("additional_rules")
            loc_rule = LOCATION_RULES.get(candidate["name"])
            if loc_rule:
                rules = f"{rules}\n\n{loc_rule}" if rules else loc_rule

            conn.execute(
                """
                INSERT INTO resume_profiles
                    (candidate_id, candidate_name, base_resume_id, base_resume_name,
                     target_roles, work_authorization, visa_status, verified_skills,
                     location_preference, open_to_relocation, keywords, additional_rules,
                     review_status, generation_status, is_match_ready, is_test_account,
                     location_gate, years_experience)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(candidate["id"]),
                    candidate["name"],
                    str(resume["id"]),
                    resume["name"],
                    json.dumps(target_roles_from_resume(resume)),
                    candidate.get("work_authorization"),
                    candidate.get("visa_status"),
                    json.dumps(list(candidate.get("verified_skills") or [])),
                    candidate.get("location_preference") or candidate.get("preferred_locations"),
                    1 if candidate.get("open_to_relocation") else 0,
                    json.dumps(active_keywords),
                    rules,
                    p.get("review_status"),
                    p.get("generation_status"),
                    1 if ready else 0,
                    1 if is_test_account else 0,
                    LOCATION_GATES.get(candidate["name"]),
                    _effective_years(candidate["name"], resume.get("content")),
                ),
            )

    print("Synced to local resume_profiles table (private, gitignored).")


if __name__ == "__main__":
    main()

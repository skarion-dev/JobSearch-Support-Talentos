"""
Matches jobs to resume profiles, following the staged design from the
Talentos masterprompt: deterministic prefilter -> cheap LLM batch scoring ->
deterministic validation -> rank. Report-only: never writes to Talentos,
only to the local keyword_jobs/resume_profiles derived tables.
"""
import json
import re
from openai import OpenAI
from app.config import LLM_CONFIG

BATCH_SIZE = 25
TOP_MATCH_MIN = 85
REVIEWABLE_MIN = 75

SENIOR_TERMS = re.compile(r"\b(senior|sr\.?|lead|principal|manager|director)\b", re.IGNORECASE)
YEARS_RE = re.compile(r"(\d+)\+?\s*(?:to\s*\d+\s*)?years?", re.IGNORECASE)

client = OpenAI(api_key=LLM_CONFIG["api_key"], base_url=LLM_CONFIG["base_url"])
MODEL = LLM_CONFIG["model"].removeprefix("openai/")


def _rule_says_reject_senior(rules_text: str | None) -> bool:
    if not rules_text:
        return False
    return "senior" in rules_text.lower() or "reject" in rules_text.lower() and "lead" in rules_text.lower()


def _rule_max_years(rules_text: str | None) -> int | None:
    if not rules_text:
        return None
    m = re.search(r"more than (\d+) years", rules_text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def prefilter(profile: dict, jobs: list[dict]) -> list[dict]:
    """Deterministic hard gates: obvious seniority/years mismatches, keyword overlap."""
    keywords = {k.lower() for k in json.loads(profile.get("keywords") or "[]")}
    rules_text = profile.get("additional_rules") or ""
    max_years = _rule_max_years(rules_text)
    reject_senior = _rule_says_reject_senior(rules_text)

    survivors = []
    for job in jobs:
        title = (job.get("title") or "")
        desc = (job.get("description") or "")
        title_lower = title.lower()

        if reject_senior and SENIOR_TERMS.search(title_lower):
            # allow through if description explicitly says junior/entry-friendly despite title
            if not re.search(r"\bentry.?level\b|\bjunior\b|\b0-\d+ years\b", desc, re.IGNORECASE):
                continue

        if max_years:
            years_mentions = [int(y) for y in YEARS_RE.findall(desc)]
            if any(y > max_years for y in years_mentions):
                continue

        # keyword overlap as a discovery signal (not a hard requirement, matches masterprompt intent)
        text = f"{title_lower} {desc.lower()}"
        overlap = sum(1 for k in keywords if k and k in text)
        job["_overlap"] = overlap
        survivors.append(job)

    survivors.sort(key=lambda j: -j["_overlap"])
    return survivors[:250]  # masterprompt section 4: keep best 100-250 per profile


PROMPT_TEMPLATE = """You are TalentOS Active Base Resume Job Matcher, evaluating jobs for ONE candidate profile.

CANDIDATE PROFILE:
- Base resume: {base_resume_name}
- Target roles: {target_roles}
- Work authorization: {work_authorization}
- Location preference: {location_preference} (open to relocation: {open_to_relocation})
- Verified skills: {verified_skills}
- Profile rules (hard gates, apply exactly): {additional_rules}

JOBS TO EVALUATE (JSON array, each has id/title/company/location/description):
{jobs_json}

For each job, score 0-100 using this rubric:
  title and role alignment                         0-30
  demonstrated tools/skills/domain coverage         0-25
  responsibilities and deliverables fit             0-20
  seniority and experience fit                      0-10
  location/work authorization fit                   0-10
  posting freshness and application viability       0-5
  subtract explicit contradiction/duplicate risk     0-25
A hard-gate failure (explicit rule violation) cannot be rescued by a high keyword score.
85-100 = TOP_MATCH; 75-84 = REVIEWABLE_MATCH; below 75 = omit entirely from output.
Do not invent evidence. Search keywords are discovery signals, not proof — cite actual job text.

Return JSON only: {{"matches": [{{"job_id": <id>, "score": <int>, "band": "TOP_MATCH"|"REVIEWABLE_MATCH", "reason": "<one concise sentence>", "matched_terms": ["..."]}}]}}
Omit jobs scoring below 75 entirely — do not include them in the output."""


def score_batch(profile: dict, jobs: list[dict]) -> list[dict]:
    jobs_compact = [
        {
            "id": j["id"],
            "title": j.get("title"),
            "company": j.get("company_name"),
            "location": j.get("location"),
            "description": (j.get("description") or "")[:1200],
        }
        for j in jobs
    ]
    prompt = PROMPT_TEMPLATE.format(
        base_resume_name=profile.get("base_resume_name"),
        target_roles=profile.get("target_roles"),
        work_authorization=profile.get("work_authorization") or "unspecified",
        location_preference=profile.get("location_preference") or "unspecified",
        open_to_relocation="yes" if profile.get("open_to_relocation") else "unspecified",
        verified_skills=profile.get("verified_skills"),
        additional_rules=profile.get("additional_rules") or "none provided",
        jobs_json=json.dumps(jobs_compact),
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        matches = data.get("matches", [])
    except Exception:
        return []

    valid_ids = {j["id"] for j in jobs}
    results = []
    for m in matches:
        try:
            job_id = int(m["job_id"])
            score = int(m["score"])
        except (KeyError, ValueError, TypeError):
            continue
        if job_id not in valid_ids or not (0 <= score <= 100):
            continue
        if score < REVIEWABLE_MIN:
            continue
        band = "TOP_MATCH" if score >= TOP_MATCH_MIN else "REVIEWABLE_MATCH"
        results.append(
            {
                "job_id": job_id,
                "score": score,
                "band": band,
                "reason": str(m.get("reason", ""))[:500],
                "matched_terms": m.get("matched_terms", []),
            }
        )
    return results


def match_profile(profile: dict, jobs: list[dict], top_n: int = 50) -> list[dict]:
    """Full staged pipeline for one profile: prefilter -> batch score -> rank -> top_n."""
    candidates = prefilter(profile, jobs)
    all_matches = []
    for i in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[i : i + BATCH_SIZE]
        all_matches.extend(score_batch(profile, batch))

    all_matches.sort(key=lambda m: -m["score"])
    return all_matches[:top_n]

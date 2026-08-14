"""
Matches jobs to resume profiles, following the staged design from the
Talentos masterprompt: deterministic prefilter -> cheap LLM batch scoring ->
deterministic validation -> rank. Report-only: never writes to Talentos,
only to the local keyword_jobs/resume_profiles derived tables.
"""
import json
import logging
import re
from openai import OpenAI
from app.config import LLM_CONFIG
from app.experience import TOLERANCE_YEARS, passes_experience_gate
from app.filters import passes_location_gate

log = logging.getLogger("matcher_agent")

BATCH_SIZE = 25
TOP_MATCH_MIN = 85
REVIEWABLE_MIN = 75

SENIOR_TERMS = re.compile(r"\b(senior|sr\.?|lead|principal|manager|director)\b", re.IGNORECASE)
YEARS_RE = re.compile(r"(\d+)\+?\s*(?:to\s*\d+\s*)?years?", re.IGNORECASE)

client = OpenAI(api_key=LLM_CONFIG["api_key"], base_url=LLM_CONFIG["base_url"])
MODEL = LLM_CONFIG["model"].removeprefix("openai/")
# Same rate ($0.14 in / $0.28 out per 1M tokens) as MODEL, so this is a free
# fallback, not a cost tradeoff. Used when MODEL itself is unavailable —
# rate-limited, over a gateway's daily budget, or erroring — rather than
# silently returning zero matches for the whole batch, which is what
# score_batch used to do on any exception at all.
FALLBACK_MODEL = "mimo-v2.5"


def _rule_says_reject_senior(rules_text: str | None) -> bool:
    if not rules_text:
        return False
    return "senior" in rules_text.lower() or "reject" in rules_text.lower() and "lead" in rules_text.lower()


def _rule_max_years(rules_text: str | None) -> int | None:
    if not rules_text:
        return None
    m = re.search(r"more than (\d+) years", rules_text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _dedupe(jobs: list[dict]) -> list[dict]:
    """
    Collapse the same posting appearing under several keywords. Without this
    one job can occupy most of a candidate's 50 slots — a DMV candidate's
    shortlist was 8 copies of one California listing.
    """
    seen, out = set(), []
    for job in jobs:
        title = re.sub(r"[^a-z0-9]+", " ", (job.get("title") or "").lower()).strip()
        company = re.sub(r"[^a-z0-9]+", " ", (job.get("company_name") or "").lower()).strip()
        key = (title, company)
        if not title or key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


def prefilter(profile: dict, jobs: list[dict]) -> list[dict]:
    """Deterministic hard gates: location, seniority/years, then keyword overlap."""
    keywords = {k.lower() for k in json.loads(profile.get("keywords") or "[]")}
    rules_text = profile.get("additional_rules") or ""
    max_years = _rule_max_years(rules_text)
    reject_senior = _rule_says_reject_senior(rules_text)
    gate = profile.get("location_gate")
    candidate_years = profile.get("years_experience")

    jobs = _dedupe(jobs)

    survivors = []
    for job in jobs:
        # Location is a hard gate enforced in code, not left to the model
        if gate and not passes_location_gate(gate, job.get("location"), job.get("title")):
            continue
        title = (job.get("title") or "")
        desc = (job.get("description") or "")
        title_lower = title.lower()

        # Same class of gate as location: 81 of 611 live TOP_MATCH rows had
        # senior/lead/principal/staff/director/manager in the title, because
        # only 4 of 18 profiles ever had additional_rules text specific
        # enough to trigger the check below, and the LLM's own rubric only
        # weighted this at 10 of ~100 points. candidate_years is computed
        # from actual resume dates (app/experience.py), not hand-typed, so
        # this applies to every profile automatically. Tolerant by design —
        # only fires on a gap bigger than TOLERANCE_YEARS, so a candidate
        # close to a "Senior" posting's implied floor still reaches the LLM
        # rather than being auto-rejected on a title word alone.
        if candidate_years is not None and not passes_experience_gate(candidate_years, title, desc):
            continue

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
- Years of experience (effective estimate — work history + education credit): {years_experience}
- Target roles: {target_roles}
- Work authorization: {work_authorization}
- Location preference: {location_preference} (open to relocation: {open_to_relocation})
- Verified skills: {verified_skills}
- Profile rules (hard gates, apply exactly): {additional_rules}

JOBS TO EVALUATE (JSON array, each has id/title/company/location/description):
{jobs_json}

For each job, score 0-100 using this rubric:
  title and role alignment                         0-25
  demonstrated tools/skills/domain coverage         0-20
  responsibilities and deliverables fit             0-15
  seniority and experience fit                      0-20
  location/work authorization fit                   0-10
  posting freshness and application viability       0-5
  subtract explicit contradiction/duplicate risk     0-25

SENIORITY IS A REAL GATE, NOT A TIEBREAKER. Compare the candidate's years above
against what the posting actually needs (an explicit "X years" figure, or the
level implied by a Senior/Lead/Principal/Staff/Director/Manager title if none
is stated). A posting needing meaningfully more experience than the candidate
has (roughly {tolerance_years}+ years short) is a hard disqualifier — score it
below 75 regardless of how well the skills line up, because that gap is a real
desk-reject with a real employer. A candidate close to the line (within about
{tolerance_years} years) is a legitimate judgment call — score it on true fit,
don't auto-reject a near-miss just because the title says "Senior."

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
    years_exp = profile.get("years_experience")
    prompt = PROMPT_TEMPLATE.format(
        base_resume_name=profile.get("base_resume_name"),
        years_experience=f"{years_exp:.1f}" if years_exp is not None else "unknown",
        target_roles=profile.get("target_roles"),
        work_authorization=profile.get("work_authorization") or "unspecified",
        location_preference=profile.get("location_preference") or "unspecified",
        open_to_relocation="yes" if profile.get("open_to_relocation") else "unspecified",
        verified_skills=profile.get("verified_skills"),
        additional_rules=profile.get("additional_rules") or "none provided",
        tolerance_years=TOLERANCE_YEARS,
        jobs_json=json.dumps(jobs_compact),
    )

    data = None
    for model in (MODEL, FALLBACK_MODEL):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            break
        except Exception as e:
            log.warning(f"{model} failed ({e}); "
                        f"{'trying ' + FALLBACK_MODEL if model == MODEL else 'giving up'}")

    if data is None:
        return []
    matches = data.get("matches", [])

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

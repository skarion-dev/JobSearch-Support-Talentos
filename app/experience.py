"""
Seniority matching — enforced in code, not left to the model.

WHY THIS EXISTS
---------------
Measured on live data: 81 of 611 TOP_MATCH rows (13%) had senior/lead/
principal/staff/director/manager explicitly in the job title. Real
examples, with the model's own stated reasoning:

    "Principal Network Engineer"          scored 98  — "a perfect match
                                                          for an experienced..."
    "Senior Design Engineer, RTL @ Google" scored 95
    "Manager, Outside Plant Engineering"   scored 96

Root cause: 14 of 18 base-resume profiles carry no seniority rule of any
kind (candidate_resume_search_profiles.additional_rules is empty free text
for most of them), and the LLM's own rubric gives "seniority and experience
fit" only 10 of ~100 points — cheap to lose against 55 points of pure
title/skill keyword overlap. This is the identical failure shape as the DMV
location bug: a rule that lives only in a prompt is not a rule.

WHAT'S DIFFERENT HERE VS THE LOCATION GATE
--------------------------------------------
Location is binary (in the zone or not). Experience is not — a candidate
with 3.5 years applying to a "Senior" posting is a real, worth-reviewing
edge case, not a bug. So this gate has a TOLERANCE: it only hard-rejects
when the gap between what the job appears to need and what the candidate
actually has is large enough that no reasonable reviewer would call it
close. Borderline gaps are deliberately let through to the LLM, which now
also gets the candidate's real computed years in the prompt instead of
nothing.

RECALIBRATED 2026-08-14, operator feedback from real outcomes
---------------------------------------------------------------
Two corrections, from real interview evidence, not theory:

  1. "Most companies are not strict on years." Maahir (1.1 years raw tenure)
     was already getting real interviews for postings stated as needing
     2-5 years — because he holds a Bachelor's, and "degree substitutes for
     experience" (e.g. "Bachelor's + 2 years, OR equivalent") is a standard,
     common hiring pattern, not an edge case. TOLERANCE_YEARS raised
     1.5 -> 3.0, and education_bonus() below adds effective years for a
     degree — generalizes automatically to every candidate with one, not
     just Maahir.

  2. Najiur computes to 6.1 years — the most senior of anyone on the roster
     by raw tenure — but operator judgment from real placement attempts is
     that he is weaker in practice than his years suggest, and should be
     steered toward roles wanting LESS experience, not more. No resume
     field can encode this — it is the opposite of what his credentials
     (he also holds a Master's) would suggest on paper. This is exactly
     what candidate_resume_search_profiles.additional_rules and
     LOCATION_GATES already exist for: operator knowledge the data can't
     show. See EXPERIENCE_OVERRIDES in sync_resume_profiles.py.

candidate_years is computed once per resume (sync_resume_profiles.py) as
years_of_experience() + education_bonus(), or an EXPERIENCE_OVERRIDES entry
when the operator has explicitly overridden it. Work history alone comes
from content.experience[].startDate/endDate, present on every base resume
and never read by anything before this fix existed.
"""
import re
from datetime import date, datetime

TOLERANCE_YEARS = 3.0

# A stated "5+ years" is a soft number employers routinely bend — hence the
# generous TOLERANCE_YEARS above. A Senior/Lead/Principal TITLE is not a
# number at all; it is a categorical statement about the level of the role,
# and no amount of "they're not strict" makes a 1.6-year candidate a senior
# hire. Verified against the audit: at the shared 3.0 tolerance, Mahbubul
# (3.8 raw) still cleared "Network Architect & Team Lead" — 1.2 short of the
# 5.0 floor and well inside 3.0. At 1.0 it correctly fails, while someone at
# 4.5 raw years still reaches a "Senior" posting as a genuine judgment call.
TITLE_TOLERANCE_YEARS = 1.0

# "Bachelor's + N years, or equivalent combination of education and
# experience" is standard phrasing in real postings — a degree substitutes
# for some amount of raw tenure. Master's/PhD implies the bachelor's too,
# so this takes the highest match, never sums across degrees.
BACHELORS_RE = re.compile(r"\bbachelor", re.IGNORECASE)
ADVANCED_DEGREE_RE = re.compile(r"\b(master|ph\.?d\.?|doctorate|mba)\b", re.IGNORECASE)
BACHELORS_BONUS_YEARS = 1.0
ADVANCED_DEGREE_BONUS_YEARS = 2.0

# Titles that imply a seniority floor even with no explicit "X years" stated.
# Split from "manager" because that word is used more loosely across
# industries (a "Project Manager" posting is often not senior-only).
SENIOR_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|director|head\s+of)\b", re.IGNORECASE
)
MANAGER_TITLE_RE = re.compile(r"\bmanager\b", re.IGNORECASE)

SENIOR_TITLE_YEARS_FLOOR = 5.0
MANAGER_TITLE_YEARS_FLOOR = 4.0

# Requires "years" to be followed by "experience" (or a close synonym) so a
# figure like "serving clients for 20 years" doesn't get read as a
# requirement. Captures the LOWER bound of a range ("3 to 5 years" -> 3),
# since that is the minimum a posting actually requires.
YEARS_EXPERIENCE_RE = re.compile(
    r"(\d{1,2})\+?\s*(?:-|to)?\s*(?:\d{1,2}\s*)?\+?\s*years?\s+"
    r"(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+|hands.on\s+|related\s+)?"
    r"experience",
    re.IGNORECASE,
)


def _parse_month(s) -> int | None:
    """Returns an absolute month index (year*12+month), or None if unparseable
    or explicitly open-ended ('Present' etc — caller treats that as ongoing)."""
    if not s:
        return None
    s = str(s).strip()
    if not s or s.lower() in ("present", "current", "now", "ongoing"):
        return None
    for fmt in ("%b %Y", "%B %Y", "%Y-%m", "%m/%Y", "%Y"):
        try:
            d = datetime.strptime(s, fmt)
            return d.year * 12 + d.month
        except ValueError:
            continue
    return None


def years_of_experience(content: dict | None) -> float | None:
    """
    Total career experience from resume content, in years, one decimal.
    Overlapping roles (concurrent jobs, an internship inside an employment
    gap) are merged rather than summed, so time isn't double-counted.
    Returns None when there's nothing to compute from — callers must treat
    that as "no signal," not zero.
    """
    if not isinstance(content, dict):
        return None
    entries = content.get("experience") or []
    now_m = date.today().year * 12 + date.today().month

    intervals = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        start = _parse_month(e.get("startDate"))
        if start is None:
            continue
        end = _parse_month(e.get("endDate")) if e.get("endDate") else None
        if end is None:
            end = now_m  # null/"Present"/unparseable end -> still ongoing
        if end < start:
            start, end = end, start
        intervals.append((start, end))

    if not intervals:
        return None

    intervals.sort()
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))

    total_months = sum(e - s for s, e in merged)
    return round(total_months / 12, 1)


def education_bonus(content: dict | None) -> float:
    """
    Effective-years credit for holding a degree, reflecting "Bachelor's + N
    years, OR equivalent" — standard phrasing in real postings. Highest
    degree only, never summed (a Master's implies the Bachelor's).
    Returns 0.0 rather than None: no degree found is a real, countable
    answer here, unlike years_of_experience() where no dates means no data.
    """
    if not isinstance(content, dict):
        return 0.0
    degrees = " | ".join(
        str(e.get("degree") or "") for e in (content.get("education") or [])
        if isinstance(e, dict)
    )
    if ADVANCED_DEGREE_RE.search(degrees):
        return ADVANCED_DEGREE_BONUS_YEARS
    if BACHELORS_RE.search(degrees):
        return BACHELORS_BONUS_YEARS
    return 0.0


def stated_years(title: str | None, description: str | None) -> float | None:
    """An explicit 'X years experience' figure from the posting, or None."""
    text = f"{title or ''} {(description or '')[:1000]}"
    nums = [int(n) for n in YEARS_EXPERIENCE_RE.findall(text)]
    plausible = [n for n in nums if 0 < n <= 20]
    return float(min(plausible)) if plausible else None


def title_implied_years(title: str | None) -> float | None:
    """The seniority floor a Senior/Lead/Principal/Staff/Director/Manager
    title implies on its own, or None when the title carries no such signal."""
    if SENIOR_TITLE_RE.search(title or ""):
        return SENIOR_TITLE_YEARS_FLOOR
    if MANAGER_TITLE_RE.search(title or ""):
        return MANAGER_TITLE_YEARS_FLOOR
    return None


def required_years(title: str | None, description: str | None) -> float | None:
    """Whichever floor is higher. Kept for reporting/dashboard use; the gate
    itself deliberately checks the two floors against different numbers —
    see passes_experience_gate."""
    stated = stated_years(title, description)
    implied = title_implied_years(title)
    if stated is not None and implied is not None:
        return max(stated, implied)
    return stated if stated is not None else implied


def passes_experience_gate(raw_years: float | None, title: str | None,
                           description: str | None,
                           tolerance: float = TOLERANCE_YEARS,
                           effective_years: float | None = None,
                           title_tolerance: float = TITLE_TOLERANCE_YEARS) -> bool:
    """
    Two floors, checked against two DIFFERENT candidate numbers under two
    DIFFERENT tolerances. Both asymmetries are the point:

      stated "X years experience"  ->  EFFECTIVE years, tolerance 3.0
          "Bachelor's + 2 years, OR equivalent combination of education and
          experience" is standard posting language, and employers bend a
          stated number routinely. A degree really does substitute here,
          which is why Maahir (1.1 raw) was landing real interviews for
          postings asking 2-5 years.

      Senior/Lead/Principal title  ->  RAW years, tolerance 1.0
          A degree does not make someone a senior hire, and a title is
          categorical rather than a negotiable number. Getting either half
          wrong is what put Tahsin (1.6 RAW years) on "Senior ASIC Design
          Engineer @ NVIDIA" and Mahbubul (3.8 raw) on "Network Architect &
          Team Lead" — 55 such rows in one night's audit, every one a
          desk-reject.

    effective_years defaults to raw_years when not supplied, so an older
    caller passing a single number keeps the stricter behaviour rather than
    silently loosening.

    Fails open on missing signal, same philosophy as passes_location_gate:
    only fires on positive evidence of a real gap.
    """
    if raw_years is None:
        return True
    if effective_years is None:
        effective_years = raw_years

    implied = title_implied_years(title)
    if implied is not None and (implied - raw_years) > title_tolerance:
        return False

    stated = stated_years(title, description)
    if stated is not None and (stated - effective_years) > tolerance:
        return False

    return True

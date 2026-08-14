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

candidate_years is computed once per resume (sync_resume_profiles.py) from
content.experience[].startDate/endDate — every base resume already carries
this, it was simply never read by anything in the matching pipeline.
"""
import re
from datetime import date, datetime

TOLERANCE_YEARS = 1.5

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


def required_years(title: str | None, description: str | None) -> float | None:
    """
    The job's own implied experience floor: an explicit 'X years experience'
    figure if one is stated, else a title-based floor for senior/lead/
    principal/staff/director/manager titles, else None (no signal at all —
    the gate must not fire on a None).
    """
    text = f"{title or ''} {(description or '')[:1000]}"
    nums = [int(n) for n in YEARS_EXPERIENCE_RE.findall(text)]
    plausible = [n for n in nums if 0 < n <= 20]
    explicit = min(plausible) if plausible else None

    title_floor = None
    if SENIOR_TITLE_RE.search(title or ""):
        title_floor = SENIOR_TITLE_YEARS_FLOOR
    elif MANAGER_TITLE_RE.search(title or ""):
        title_floor = MANAGER_TITLE_YEARS_FLOOR

    if explicit is not None and title_floor is not None:
        return max(explicit, title_floor)
    return explicit if explicit is not None else title_floor


def passes_experience_gate(candidate_years: float | None, title: str | None,
                           description: str | None,
                           tolerance: float = TOLERANCE_YEARS) -> bool:
    """
    Fails open on missing signal (no computed candidate years, or nothing in
    the posting implying a seniority floor) — this gate only fires on
    positive evidence of a real gap, same philosophy as passes_location_gate.
    """
    if candidate_years is None:
        return True
    req = required_years(title, description)
    if req is None:
        return True
    return (req - candidate_years) <= tolerance

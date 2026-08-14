"""
Hardcoded rule: only keep jobs located in the USA (or explicitly US-remote).
Applied to every job regardless of which scrape method produced it.
"""
import re

US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

US_TEXT_HINTS = ("usa", "u.s.a", "united states", "u.s.", " us ", "us-remote", "remote - us", "remote, us")

NON_US_HINTS = (
    "canada", "united kingdom", "uk", "germany", "france", "india", "mexico",
    "brazil", "colombia", "argentina", "spain", "italy", "netherlands", "poland",
    "philippines", "singapore", "australia", "ireland", "portugal", "japan",
    "china", "vietnam", "pakistan", "bangladesh", "nigeria", "egypt", "uae",
    "saudi arabia", "south africa", "sweden", "switzerland", "belgium", "austria",
    "gbr", "col", "mex", "bra", "arg", "esp", "ita", "nld", "pol", "phl", "sgp",
    "aus", "irl", "prt", "jpn", "chn", "vnm", "pak", "bgd", "nga", "egy", "are",
)

STATE_PATTERN = re.compile(r"\b(" + "|".join(US_STATE_ABBR) + r")\b")

# CJK / Cyrillic in a company or title is a non-US posting whose LOCATION
# string still looked American. An FPGA role at カールストルツ・エンドスコピー・
# ジャパン（株） reached a candidate's shortlist this way — location alone said
# nothing disqualifying, so the text itself has to be checked too.
NON_LATIN_RE = re.compile(r"[　-鿿＀-￯Ѐ-ӿ]")


def is_us_job(location: str | None, title: str | None = None,
              company: str | None = None) -> bool:
    """Strict allowlist: only pass jobs with a clear US signal, reject everything else."""
    if not location:
        return False

    if NON_LATIN_RE.search(f"{title or ''} {company or ''}"):
        return False

    loc = location.strip().lower()

    if any(hint in loc for hint in NON_US_HINTS):
        return False

    if any(hint in loc for hint in US_TEXT_HINTS):
        return True

    if STATE_PATTERN.search(location.upper()):
        return True

    return False


def filter_us_jobs(jobs: list[dict]) -> list[dict]:
    return [j for j in jobs
            if is_us_job(j.get("location"), j.get("title"), j.get("company_name"))]


# --- unwinnable / low-value postings ----------------------------------------
# Both enforced in code rather than left to the scorer: an LLM reading "TS/SCI
# required" against a strong skills match will still hand back 90+, and a
# 13%-of-shortlist audit showed exactly that happening.

CLEARANCE_RE = re.compile(
    r"\b(ts/sci|top\s+secret|secret\s+clearance|security\s+clearance|"
    r"polygraph|q\s+clearance|active\s+clearance)\b", re.IGNORECASE)

# Recorded authorizations that make a US federal clearance realistic. Anything
# else on this roster (EAD, H-1B, OPT, or simply unrecorded) cannot hold one,
# so those postings are an AE's time spent on a guaranteed rejection.
CLEARANCE_ELIGIBLE_AUTH = re.compile(r"\b(citizen|us\s*citizen|green\s*card|permanent\s*resident)\b",
                                     re.IGNORECASE)

INTERN_RE = re.compile(r"\b(intern|internship|trainee|co-op|apprentice)\b", re.IGNORECASE)


def requires_clearance(title: str | None, description: str | None) -> bool:
    text = f"{title or ''} {(description or '')[:2000]}"
    return bool(CLEARANCE_RE.search(text))


def clearance_eligible(work_authorization: str | None, visa_status: str | None) -> bool:
    """Conservative: only an explicitly recorded citizen/green-card status
    qualifies. Unrecorded (NULL upstream for most of this roster) is treated
    as ineligible — the cost of wrongly skipping one posting is far lower
    than an AE working a req the candidate legally cannot hold."""
    return bool(CLEARANCE_ELIGIBLE_AUTH.search(f"{work_authorization or ''} {visa_status or ''}"))


def is_intern_or_trainee(title: str | None) -> bool:
    """Low value for a placement business: student-gated, short-term, and
    usually residency-restricted."""
    return bool(INTERN_RE.search(title or ""))


# --- regional gates ---------------------------------------------------------
# A location rule written only into the LLM prompt is not a gate: the model
# returned 8 California jobs for a DMV-only candidate because "Flex" in the
# title read as remote. Regional limits are enforced in code instead.

REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b|\bwork from home\b|\bwfh\b", re.IGNORECASE)

# DC / Maryland / Northern Virginia within roughly 100 miles
DMV_STATES = {"DC", "MD", "VA", "WV", "DE"}
DMV_CITIES = (
    "washington", "arlington", "alexandria", "bethesda", "rockville", "fairfax",
    "reston", "herndon", "tysons", "mclean", "vienna", "annandale", "springfield",
    "woodbridge", "manassas", "gainesville", "leesburg", "sterling", "ashburn",
    "silver spring", "gaithersburg", "frederick", "columbia", "baltimore",
    "annapolis", "bowie", "laurel", "greenbelt", "hyattsville", "college park",
    "germantown", "waldorf", "jessup", "dulles", "quantico", "fredericksburg",
    "richmond", "henrico", "chantilly", "centreville", "burke", "lorton",
    "district of columbia", "prince george", "montgomery county", "loudoun",
    "anne arundel", "howard county", "fauquier", "stafford", "prince william",
)
# Match state codes in their ORIGINAL case. Uppercasing first made "De Pere,
# Brown County" (Wisconsin) read as Delaware and pass a DMV-only gate.
# Real state codes are uppercase in these feeds; "De" in a city name is not.
_STATE_TOKEN = re.compile(r"\b([A-Z]{2})\b")


def is_remote(location: str | None, title: str | None = None) -> bool:
    blob = f"{location or ''} {title or ''}"
    return bool(REMOTE_RE.search(blob))


def in_dmv(location: str | None) -> bool:
    if not location:
        return False
    loc = location.lower()
    states = set(_STATE_TOKEN.findall(location))   # original case, not uppercased
    # An explicit non-DMV state disqualifies, even if a city name collides
    if states and not (states & DMV_STATES):
        return False
    if states & DMV_STATES:
        return True
    return any(city in loc for city in DMV_CITIES)


GATES = {
    # DMV commute radius, or fully remote
    "dmv_or_remote": lambda loc, title: in_dmv(loc) or is_remote(loc, title),
}


def passes_location_gate(gate: str | None, location: str | None, title: str | None) -> bool:
    if not gate:
        return True
    fn = GATES.get(gate)
    return fn(location, title) if fn else True

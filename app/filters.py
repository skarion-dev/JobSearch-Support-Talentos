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


def is_us_job(location: str | None) -> bool:
    """Strict allowlist: only pass jobs with a clear US signal, reject everything else."""
    if not location:
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
    return [j for j in jobs if is_us_job(j.get("location"))]

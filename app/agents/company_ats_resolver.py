"""
Resolve a company name to its ATS job board WITHOUT using a search engine.

Why this exists
---------------
Recovering the real posting URL by web-searching every job hit search-engine
rate limits hard (36% coverage before 429s). But 18k jobs come from only ~3.7k
companies, and the top 500 companies cover ~67% of the corpus. So resolve the
COMPANY once, not the job.

Resolution is deterministic URL probing against known ATS hosts using slug
variants of the company name. No search engine is involved, so there is
nothing to rate-limit us. A hit yields the board's public API, which returns
exact apply URLs AND full descriptions — fixing both gaps in one pass.

Results are cached in company_ats so each company is probed at most once.
"""
import re
import requests

from app.agents.ats_detectors import FETCHERS

TIMEOUT = 12

_SUFFIXES = re.compile(
    r"\b(inc|llc|corp|corporation|co|ltd|limited|company|group|holdings|plc|llp|the)\b\.?",
    re.IGNORECASE,
)


def slug_variants(company_name: str) -> list[str]:
    """Plausible board slugs for a company, most-likely first."""
    base = _SUFFIXES.sub(" ", company_name or "").strip()
    base = re.sub(r"[^A-Za-z0-9 ]+", " ", base)
    words = [w for w in base.split() if w]
    if not words:
        return []

    joined = "".join(words).lower()
    hyphen = "-".join(words).lower()
    first = words[0].lower()

    out = []
    for v in (joined, hyphen, first):
        if v and v not in out and len(v) > 2:
            out.append(v)
    return out


# (method_type, url template, config builder) — probed in this order
PROBES = [
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
     lambda s: {"board_token": s}),
    ("lever", "https://api.lever.co/v0/postings/{s}?mode=json&limit=1",
     lambda s: {"company_slug": s}),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/{s}",
     lambda s: {"board": s}),
    ("recruitee", "https://{s}.recruitee.com/api/offers/",
     lambda s: {"company": s}),
    ("smartrecruiters", "https://api.smartrecruiters.com/v1/companies/{s}/postings",
     lambda s: {"company": s}),
]


def _looks_populated(method_type: str, payload) -> bool:
    """A 200 with an empty board is not a match — avoid false positives."""
    try:
        if method_type == "greenhouse":
            return len(payload.get("jobs", [])) > 0
        if method_type == "lever":
            return isinstance(payload, list) and len(payload) > 0
        if method_type == "ashby":
            return len(payload.get("jobs", [])) > 0
        if method_type == "recruitee":
            return len(payload.get("offers", [])) > 0
        if method_type == "smartrecruiters":
            return payload.get("totalFound", 0) > 0
    except Exception:
        return False
    return False


def resolve(company_name: str) -> tuple[str, dict] | tuple[None, None]:
    """Probe ATS hosts for this company. Returns (method_type, config) or (None, None)."""
    for slug in slug_variants(company_name):
        for method_type, url_tmpl, cfg in PROBES:
            try:
                resp = requests.get(url_tmpl.format(s=slug), timeout=TIMEOUT)
            except requests.RequestException:
                continue
            if resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except ValueError:
                continue
            if _looks_populated(method_type, payload):
                return method_type, cfg(slug)
    return None, None


def fetch_board(method_type: str, config: dict) -> list[dict]:
    """Fetch every posting from a resolved board (direct URLs + full descriptions)."""
    fetcher = FETCHERS.get(method_type)
    if not fetcher:
        return []
    try:
        return fetcher(config)
    except Exception:
        return []


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"\(.*?\)", " ", t)          # strip parentheticals
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\b(senior|sr|junior|jr|lead|staff|principal|i{1,3}|iv|v)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()

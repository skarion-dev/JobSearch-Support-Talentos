"""
Adzuna aggregator: instead of visiting each company's own site (which
doesn't scale to millions of companies), pull recent US job postings in
bulk from Adzuna and match each posting's employer name against our
companies table. One API call surfaces postings for many companies at once.
"""
import re
import threading
import requests
from app.config import ADZUNA_APP_ID, ADZUNA_APP_KEY, ADZUNA_ACCOUNTS

BASE_URL = "https://api.adzuna.com/v1/api/jobs/us/search"
RESULTS_PER_PAGE = 50

# --- credential rotation ---------------------------------------------------
# Adzuna's free tier is ~250 calls/account/day. When an account starts
# returning 429, retire it for this process and move to the next one.
_cred_lock = threading.Lock()
_cred_index = 0
_exhausted: set[int] = set()


def _current_creds() -> tuple[str, str]:
    with _cred_lock:
        if not ADZUNA_ACCOUNTS:
            raise RuntimeError("No ADZUNA_APP_ID / ADZUNA_APP_KEY configured in .env")
        return ADZUNA_ACCOUNTS[_cred_index]


def _retire_current(idx: int) -> bool:
    """Mark the account exhausted and advance. Returns True if another remains."""
    global _cred_index
    with _cred_lock:
        _exhausted.add(idx)
        for i in range(len(ADZUNA_ACCOUNTS)):
            if i not in _exhausted:
                _cred_index = i
                return True
        return False


def _get(page: int, extra_params: dict) -> list[dict]:
    """GET a search page, rotating credentials on quota exhaustion."""
    while True:
        with _cred_lock:
            idx = _cred_index
        app_id, app_key = _current_creds()

        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": RESULTS_PER_PAGE,
            "sort_by": "date",
            "content-type": "application/json",
            **extra_params,
        }
        resp = requests.get(f"{BASE_URL}/{page}", params=params, timeout=20)

        if resp.status_code in (429, 403):
            if _retire_current(idx):
                continue  # retry immediately on the next account
            resp.raise_for_status()

        resp.raise_for_status()
        return resp.json().get("results", [])


def credential_status() -> dict:
    with _cred_lock:
        return {
            "accounts_configured": len(ADZUNA_ACCOUNTS),
            "active_index": _cred_index,
            "exhausted": sorted(_exhausted),
        }

_SUFFIXES = re.compile(
    r"\b(inc|llc|corp|corporation|co|ltd|company|group|holdings|plc|llp|the)\b\.?",
    re.IGNORECASE,
)


def normalize_company_name(name: str | None) -> str:
    if not name:
        return ""
    n = name.lower()
    n = _SUFFIXES.sub(" ", n)
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def fetch_page(page: int, max_days_old: int = 10) -> list[dict]:
    return _get(page, {"max_days_old": max_days_old})


def fetch_by_keyword(keyword: str, page: int = 1, max_days_old: int = 3) -> list[dict]:
    """Search Adzuna for a specific keyword (job title/skill), not a bulk date browse."""
    return _get(page, {"what": keyword, "max_days_old": max_days_old})


def fetch_by_keyword_all(keyword: str, max_days_old: int = 3, max_pages: int = 5) -> tuple[list[dict], int]:
    """
    Paginate through all results for a keyword (up to max_pages), stopping early
    once a page returns fewer than a full page of results. Returns (results, calls_made).
    """
    all_results = []
    calls_made = 0
    for page in range(1, max_pages + 1):
        results = fetch_by_keyword(keyword, page=page, max_days_old=max_days_old)
        calls_made += 1
        all_results.extend(results)
        if len(results) < RESULTS_PER_PAGE:
            break
    return all_results, calls_made


def to_job(result: dict) -> dict:
    company_name = (result.get("company") or {}).get("display_name")
    location = (result.get("location") or {}).get("display_name")
    salary = None
    if result.get("salary_min") or result.get("salary_max"):
        lo, hi = result.get("salary_min"), result.get("salary_max")
        salary = f"${lo:,.0f}-${hi:,.0f}" if lo and hi else f"${(lo or hi):,.0f}"

    return {
        "company_name": company_name,
        "title": result.get("title"),
        "location": location,
        "job_url": result.get("redirect_url"),
        "posted_date": (result.get("created") or "")[:10] or None,
        "description": result.get("description"),
        "salary": salary,
        "remote": None,
        "source": "adzuna",
    }


def fetch_and_match(company_index: dict[str, int], max_pages: int = 20, max_days_old: int = 10):
    """
    company_index: {normalized_company_name: company_id}, built once by the caller.
    Yields (company_id, job_dict) for every posting whose employer matches a known company.
    """
    for page in range(1, max_pages + 1):
        try:
            results = fetch_page(page, max_days_old=max_days_old)
        except requests.RequestException:
            break
        if not results:
            break

        for result in results:
            job = to_job(result)
            key = normalize_company_name(job["company_name"])
            company_id = company_index.get(key)
            if company_id:
                yield company_id, job

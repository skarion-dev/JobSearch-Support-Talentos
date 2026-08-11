"""
Adzuna aggregator: instead of visiting each company's own site (which
doesn't scale to millions of companies), pull recent US job postings in
bulk from Adzuna and match each posting's employer name against our
companies table. One API call surfaces postings for many companies at once.
"""
import re
import requests
from app.config import ADZUNA_APP_ID, ADZUNA_APP_KEY

BASE_URL = "https://api.adzuna.com/v1/api/jobs/us/search"
RESULTS_PER_PAGE = 50

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
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        raise RuntimeError("ADZUNA_APP_ID / ADZUNA_APP_KEY not set in .env")

    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": RESULTS_PER_PAGE,
        "sort_by": "date",
        "max_days_old": max_days_old,
        "content-type": "application/json",
    }
    resp = requests.get(f"{BASE_URL}/{page}", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("results", [])


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

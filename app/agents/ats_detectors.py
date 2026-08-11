"""
Deterministic, LLM-free extractors for common ATS platforms. These have
stable public JSON APIs, so once we know a company uses one, we never
need to call an LLM for that company again.
"""
import re
import requests

TIMEOUT = 15


def detect_platform(url: str) -> tuple[str, dict] | tuple[None, None]:
    """Return (method_type, method_config) if url matches a known ATS, else (None, None)."""
    if not url:
        return None, None

    m = re.search(r"boards\.greenhouse\.io/([^/?#]+)", url)
    if m:
        return "greenhouse", {"board_token": m.group(1)}

    m = re.search(r"jobs\.lever\.co/([^/?#]+)", url)
    if m:
        return "lever", {"company_slug": m.group(1)}

    m = re.search(r"([a-zA-Z0-9\-]+)\.(wd\d*)\.myworkdayjobs\.com/([^/?#]+)", url)
    if m:
        return "workday", {"tenant": m.group(1), "wd": m.group(2), "site": m.group(3)}

    return None, None


def fetch_greenhouse(board_token: str) -> list[dict]:
    resp = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs",
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(
            {
                "title": j.get("title"),
                "location": (j.get("location") or {}).get("name"),
                "job_url": j.get("absolute_url"),
                "posted_date": (j.get("updated_at") or "")[:10] or None,
                "description": None,
                "remote": None,
                "salary": None,
            }
        )
    return jobs


def fetch_lever(company_slug: str) -> list[dict]:
    resp = requests.get(
        f"https://api.lever.co/v0/postings/{company_slug}?mode=json", timeout=TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data:
        created_ms = j.get("createdAt")
        posted_date = None
        if created_ms:
            from datetime import datetime, timezone
            posted_date = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).date().isoformat()
        jobs.append(
            {
                "title": j.get("text"),
                "location": (j.get("categories") or {}).get("location"),
                "job_url": j.get("hostedUrl"),
                "posted_date": posted_date,
                "description": None,
                "remote": None,
                "salary": None,
            }
        )
    return jobs


def fetch_workday(tenant: str, site: str, wd: str = "wd3") -> list[dict]:
    """Workday's job list is behind a JSON POST endpoint keyed by tenant/site."""
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    resp = requests.post(url, json={"limit": 50, "offset": 0, "searchText": ""}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data.get("jobPostings", []):
        jobs.append(
            {
                "title": j.get("title"),
                "location": j.get("locationsText"),
                "job_url": f"https://{tenant}.{wd}.myworkdayjobs.com{j.get('externalPath', '')}",
                "posted_date": None,
                "description": None,
                "remote": None,
                "salary": None,
            }
        )
    return jobs


FETCHERS = {
    "greenhouse": lambda cfg: fetch_greenhouse(cfg["board_token"]),
    "lever": lambda cfg: fetch_lever(cfg["company_slug"]),
    "workday": lambda cfg: fetch_workday(cfg["tenant"], cfg["site"], cfg.get("wd", "wd3")),
}


def fetch_with_method(method_type: str, method_config: dict) -> list[dict]:
    fetcher = FETCHERS.get(method_type)
    if not fetcher:
        raise ValueError(f"No deterministic fetcher for method_type={method_type}")
    return fetcher(method_config)

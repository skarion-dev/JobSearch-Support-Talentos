"""
Finder agent: for a company with no known careers page, first try common
careers-page URL paths on their own domain (fast, free, no network search).
Only fall back to a web search if none of those resolve.
"""
import requests
from ddgs import DDGS

CAREERS_HINTS = ("career", "careers", "jobs", "job-openings", "join-us", "work-with-us", "opportunities")

COMMON_PATHS = [
    "/careers", "/careers/", "/jobs", "/jobs/", "/careers.html",
    "/about/careers", "/company/careers", "/en/careers", "/join-us",
    "/work-with-us", "/about-us/careers", "/en-us/careers",
]

TIMEOUT = 6


def _normalize(website: str) -> str:
    website = website.strip()
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    return website.rstrip("/")


def _probe_common_paths(website: str) -> str | None:
    base = _normalize(website)
    for path in COMMON_PATHS:
        url = base + path
        try:
            resp = requests.head(url, timeout=TIMEOUT, allow_redirects=True)
            if resp.status_code == 405:  # some servers reject HEAD
                resp = requests.get(url, timeout=TIMEOUT, allow_redirects=True)
            if resp.status_code < 400:
                return resp.url
        except requests.RequestException:
            continue
    return None


def _search_web(company_name: str) -> str | None:
    query = f"{company_name} careers jobs"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5, backend="duckduckgo"))
    except Exception:
        results = []

    for r in results:
        url = r.get("href") or r.get("url") or ""
        if any(hint in url.lower() for hint in CAREERS_HINTS):
            return url

    if results:
        return results[0].get("href") or results[0].get("url")

    return None


def find_careers_url(company_name: str, website: str | None = None) -> str | None:
    if website:
        found = _probe_common_paths(website)
        if found:
            return found

    found = _search_web(company_name)
    if found:
        return found

    return website

"""
Finder agent: for a company with no known careers page, search the web
to locate one (or fall back to their homepage) before the scraper agent runs.
"""
from ddgs import DDGS

CAREERS_HINTS = ("career", "careers", "jobs", "job-openings", "join-us", "work-with-us", "opportunities")


def find_careers_url(company_name: str, website: str | None = None) -> str | None:
    query = f"{company_name} careers jobs"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
    except Exception:
        results = []

    for r in results:
        url = r.get("href") or r.get("url") or ""
        if any(hint in url.lower() for hint in CAREERS_HINTS):
            return url

    if results:
        return results[0].get("href") or results[0].get("url")

    return website

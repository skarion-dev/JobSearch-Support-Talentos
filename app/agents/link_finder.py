"""
Find a plausible direct job-posting link via web search (title + company),
since Adzuna's own redirect links are gated behind a login wall and don't
expose the real employer/application URL.
"""
import time
from ddgs import DDGS

BAD_DOMAINS = ("adzuna.com",)


def find_source_url(title: str, company_name: str | None, retries: int = 2) -> str | None:
    if not title:
        return None
    query = f'"{title}" {company_name}' if company_name else title

    for attempt in range(retries + 1):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
            for r in results:
                url = r.get("href") or r.get("url") or ""
                if url and not any(bad in url.lower() for bad in BAD_DOMAINS):
                    return url
            return None
        except Exception:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
    return None

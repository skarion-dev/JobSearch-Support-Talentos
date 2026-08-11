"""
Find a plausible direct job-posting link via web search (title + company),
since Adzuna's own redirect links are gated behind a login wall and don't
expose the real employer/application URL.
"""
import random
import time
import threading
from ddgs import DDGS

BAD_DOMAINS = ("adzuna.com",)

# Global politeness throttle. Search backends rate-limit on aggregate request
# rate, not per-worker, so the gate has to be process-wide: bursting 25 workers
# is what produced 429s and capped coverage at 36%.
_throttle_lock = threading.Lock()
_last_request_at = 0.0
MIN_INTERVAL = 0.7  # seconds between search requests, process-wide


def _wait_turn():
    global _last_request_at
    with _throttle_lock:
        now = time.monotonic()
        delta = now - _last_request_at
        if delta < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - delta)
        _last_request_at = time.monotonic()


def find_source_url(title: str, company_name: str | None, retries: int = 3) -> str | None:
    if not title:
        return None
    query = f'"{title}" {company_name}' if company_name else title

    for attempt in range(retries + 1):
        _wait_turn()
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
                # exponential backoff with jitter so retries don't resynchronize
                time.sleep((2 ** attempt) + random.uniform(0, 1.5))
                continue
            return None
    return None

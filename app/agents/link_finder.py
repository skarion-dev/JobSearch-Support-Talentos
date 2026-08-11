"""
Find a plausible direct job-posting link via web search (title + company),
since Adzuna's own redirect links are gated behind a login wall and don't
expose the real employer/application URL.
"""
import random
import re
import time
import threading
from ddgs import DDGS

BAD_DOMAINS = ("adzuna.com",)

# Domain preference. Measured on 737 recovered links: the big aggregators
# return an anti-bot shell to a plain HTTP fetch, so a description can never be
# extracted from them (LinkedIn 229/245 failed, Indeed 102/102, CareerBuilder
# 30/30, Monster 25/25, ZipRecruiter 24/24). ATS hosts and employer career
# sites extract cleanly. So when the search returns several candidates, take
# the one we can actually read rather than the first one listed.

ATS_HOSTS = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.com",
    "ashbyhq.com", "smartrecruiters.com", "workable.com", "recruitee.com",
    "bamboohr.com", "icims.com", "jobvite.com", "taleo.net", "paylocity.com",
    "successfactors.com", "applytojob.com", "breezy.hr", "teamtailor.com",
    "oraclecloud.com", "silkroad.com", "clearcompany.com",
)

# Scrapeable, but a middleman rather than the employer
NEUTRAL_BOARDS = ("simplify.jobs", "builtin.com", "dice.com", "clearancejobs.com")

# Reachable by a human, but not by an HTTP fetch — last resort only
BLOCKED_BOARDS = (
    "linkedin.com", "indeed.com", "glassdoor.com", "monster.com",
    "ziprecruiter.com", "careerbuilder.com", "talent.com", "jooble.org",
    "careerjet.com", "snagajob.com", "simplyhired.com", "lensa.com",
)


def _rank(url: str, company_name: str | None) -> int:
    """Lower is better."""
    u = url.lower()
    if any(h in u for h in ATS_HOSTS):
        return 0                                  # direct apply page, scrapeable
    if company_name:
        # employer's own site: first significant word of the company name in host
        host = re.sub(r"^https?://", "", u).split("/")[0]
        for word in re.findall(r"[a-z0-9]{4,}", company_name.lower()):
            if word in host:
                return 1
    if any(b in u for b in BLOCKED_BOARDS):
        return 4                                  # readable by humans, not by us
    if any(b in u for b in NEUTRAL_BOARDS):
        return 3
    return 2                                      # unknown host: worth a try

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
                results = list(ddgs.text(query, max_results=8))
            urls = [
                (r.get("href") or r.get("url") or "")
                for r in results
            ]
            urls = [u for u in urls if u and not any(b in u.lower() for b in BAD_DOMAINS)]
            if not urls:
                return None
            # prefer a link we can actually read over whatever ranked first
            urls.sort(key=lambda u: _rank(u, company_name))
            return urls[0]
        except Exception:
            if attempt < retries:
                # exponential backoff with jitter so retries don't resynchronize
                time.sleep((2 ** attempt) + random.uniform(0, 1.5))
                continue
            return None
    return None

"""
Apify job ingestion — the fallback for boards a plain HTTP fetch cannot read.

Why
---
Measured on 737 recovered links, the big aggregators return an anti-bot shell
to a direct fetch, so a description can never be extracted from them:
  linkedin 229/245 failed, indeed 102/102, careerbuilder 30/30,
  monster 25/25, ziprecruiter 24/24, glassdoor 26/28.

Apify actors run a real browser behind residential proxies and return the FULL
description plus the platform job id and apply URL. This mirrors the workflow
Talentos already runs (cheap_scraper/linkedin-job-scraper, keyword array,
locations, publishedAt window, saveOnlyUniqueItems).

Token rotation
--------------
Talentos keeps a pool of ~44 free-tier tokens and rotates as each allowance is
spent. Same behaviour here: on 401/402/403/429 the token is retired for the
process and the call retries on the next one.
"""
import os
import threading
import time

import requests

BASE = "https://api.apify.com/v2"
POLL_INTERVAL = 10
DEFAULT_TIMEOUT = 900


def _load_tokens() -> list[str]:
    toks, i = [], 1
    while True:
        t = os.getenv(f"APIFY_TOKEN_{i}")
        if not t:
            break
        toks.append(t)
        i += 1
    single = os.getenv("APIFY_TOKEN")
    if single and single not in toks:
        toks.insert(0, single)
    return toks


TOKENS = _load_tokens()
_lock = threading.Lock()
_idx = 0
_dead: set[int] = set()


def _current() -> tuple[int, str]:
    with _lock:
        if not TOKENS:
            raise RuntimeError("No APIFY_TOKEN_* configured — run scripts.sync_apify_tokens")
        return _idx, TOKENS[_idx]


def _retire(i: int) -> bool:
    global _idx
    with _lock:
        _dead.add(i)
        for j in range(len(TOKENS)):
            if j not in _dead:
                _idx = j
                return True
        return False


def token_status() -> dict:
    with _lock:
        return {"total": len(TOKENS), "active_index": _idx, "retired": len(_dead)}


# --- actor registry ---------------------------------------------------------
# Each entry maps our generic request onto that actor's input shape, and its
# output rows onto our common job shape.

def _txt(v):
    """Actors are inconsistent: a field may come back as str, list, or dict."""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list):
        parts = [_txt(x) for x in v]
        joined = "\n".join(p for p in parts if p)
        return joined or None
    if isinstance(v, dict):
        for k in ("text", "description", "value", "content", "name"):
            if k in v:
                return _txt(v[k])
        return None
    return str(v)


def _li_input(keywords, max_items, days):
    # LinkedIn only accepts its own f_TPR windows — 24h, week, month. An
    # arbitrary value like r259200 (3 days) is rejected with a 400, so snap up
    # to the next valid window and filter precisely on posted_date afterwards.
    if days <= 1:
        window = "r86400"
    elif days <= 7:
        window = "r604800"
    else:
        window = "r2592000"
    return {
        "keyword": keywords,
        "locations": ["United States"],
        "maxItems": max_items,
        "publishedAt": window,
        "saveOnlyUniqueItems": True,
    }


def _li_map(r):
    return {
        "title": _txt(r.get("jobTitle")),
        "company_name": _txt(r.get("companyName")),
        "location": _txt(r.get("location")),
        "description": _txt(r.get("jobDescription")),
        "job_url": _txt(r.get("jobUrl")),
        "apply_url": _txt(r.get("applyUrl")) or _txt(r.get("jobUrl")),
        "external_job_id": str(r["jobId"]) if r.get("jobId") else None,
        "posted_date": (_txt(r.get("publishedAt")) or "")[:10] or None,
        "company_url": _txt(r.get("companyUrl")),
        "salary": _txt(r.get("salaryInfo")),
        "source": "apify:linkedin",
    }


def _indeed_input(keywords, max_items, days):
    # Indeed's query parser returns nothing for an OR-joined keyword list, so
    # this actor must be driven ONE keyword per run (ingest uses chunk=1).
    # country is a lowercase ISO enum; datePosted only accepts 1/3/7/14.
    window = min([1, 3, 7, 14], key=lambda a: abs(a - days))
    return {
        "title": keywords[0],
        "country": "us",
        "location": "United States",
        "limit": max_items,
        "datePosted": str(window),
    }


def _indeed_location(loc) -> str | None:
    """Indeed returns a structured location object, not a display string."""
    if isinstance(loc, str):
        return loc.strip() or None
    if not isinstance(loc, dict):
        return None
    city = loc.get("city") or loc.get("formatted", {}).get("short") if isinstance(loc.get("formatted"), dict) else loc.get("city")
    state = loc.get("admin1Code") or loc.get("admin1") or loc.get("stateProvince")
    country = loc.get("countryCode") or loc.get("countryName")
    parts = [p for p in (city, state, "USA" if country in ("US", "United States") else country) if p]
    return ", ".join(str(p) for p in parts) or None


def _indeed_salary(sal) -> str | None:
    if not isinstance(sal, dict):
        return _txt(sal)
    rng = sal.get("range") if isinstance(sal.get("range"), dict) else sal
    lo, hi = rng.get("min"), rng.get("max")
    if lo and hi:
        return f"{lo:,.0f}-{hi:,.0f} {sal.get('unitOfWork') or ''}".strip()
    return _txt(lo or hi)


def _indeed_map(r):
    employer = r.get("employer") if isinstance(r.get("employer"), dict) else {}
    return {
        "title": _txt(r.get("title")),
        "company_name": _txt(employer.get("name")) or _txt(r.get("company")) or _txt(r.get("companyName")),
        "location": _indeed_location(r.get("location")),
        "description": _txt(r.get("description")),
        "job_url": _txt(r.get("jobUrl")) or _txt(r.get("url")),
        "apply_url": _txt(r.get("url")) or _txt(r.get("jobUrl")),
        "external_job_id": str(r.get("key") or r.get("refNum") or "") or None,
        "posted_date": (_txt(r.get("datePublished")) or "")[:10] or None,
        "company_url": _txt(employer.get("relativeCompanyPageUrl")),
        "salary": _indeed_salary(r.get("baseSalary")),
        "source": "apify:indeed",
    }


def _google_input(keywords, max_items, days):
    return {
        "query": " OR ".join(f'"{k}"' for k in keywords[:6]),
        "location": "United States",
        "country": "us",
        "num_results": max_items,
        "max_pagination": 5,
    }


def _google_map(r):
    # Google Jobs aggregates other boards; "via" names the originating source,
    # and apply_options often contains the employer's own career-page link.
    apply_url = _txt(r.get("apply_link")) or _txt(r.get("share_link"))
    opts = r.get("apply_options")
    if isinstance(opts, list) and opts:
        first = opts[0]
        if isinstance(first, dict):
            apply_url = _txt(first.get("link")) or apply_url
    return {
        "title": _txt(r.get("title")),
        "company_name": _txt(r.get("company_name")) or _txt(r.get("company")),
        "location": _txt(r.get("location")),
        "description": _txt(r.get("description")) or _txt(r.get("job_description")),
        "job_url": apply_url or _txt(r.get("share_link")),
        "apply_url": apply_url,
        "external_job_id": _txt(r.get("job_id")),
        "posted_date": None,   # Google gives relative text ("3 days ago"), not a date
        "company_url": None,
        "salary": _txt(r.get("salary")),
        "source": "apify:google",
    }


ACTORS = {
    # Proven in Talentos production (actId 2rJKkhh7vjpX7pvjg)
    "linkedin": {
        "actor": "cheap_scraper~linkedin-job-scraper",
        "build_input": _li_input,
        "map_row": _li_map,
    },
    # 5.0 rating, 99.9% success, 23k users, ~$0.0001/job
    "indeed": {
        "actor": "valig~indeed-jobs-scraper",
        "build_input": _indeed_input,
        "map_row": _indeed_map,
    },
    # Google Jobs aggregates many boards and exposes the originating source,
    # which often resolves to the employer's own career page.
    # 5.0 rating, 99.7% success, ~$0.015/result, no per-page fee.
    "google": {
        "actor": "johnvc~google-jobs-scraper---pay-per-result",
        "build_input": _google_input,
        "map_row": _google_map,
    },
    # fallback if the primary Indeed actor degrades
    "indeed_alt": {
        "actor": "borderline~indeed-scraper",
        "build_input": lambda kws, n, d: {
            "query": " OR ".join(kws[:10]), "country": "US",
            "location": "United States", "maxRows": n, "fromDays": str(d),
        },
        "map_map": None,
        "map_row": _indeed_map,
    },
}


def run_actor(source: str, keywords: list[str], max_items: int = 200,
              days: int = 7, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Run an actor to completion and return rows mapped to our common shape."""
    spec = ACTORS[source]
    payload = spec["build_input"](keywords, max_items, days)

    while True:
        idx, token = _current()
        start = requests.post(
            f"{BASE}/acts/{spec['actor']}/runs",
            params={"token": token}, json=payload, timeout=60,
        )
        if start.status_code in (401, 402, 403, 429):
            if _retire(idx):
                continue
            start.raise_for_status()
        start.raise_for_status()
        run = start.json()["data"]
        break

    run_id, ds_id = run["id"], run["defaultDatasetId"]
    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        st = requests.get(f"{BASE}/actor-runs/{run_id}", params={"token": token}, timeout=30)
        if st.status_code != 200:
            continue
        status = st.json()["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
    else:
        status = "TIMED-OUT"

    items = requests.get(
        f"{BASE}/datasets/{ds_id}/items",
        params={"token": token, "clean": "true", "limit": max_items}, timeout=120,
    )
    if items.status_code != 200:
        return []

    rows = []
    for r in items.json():
        try:
            mapped = spec["map_row"](r)
        except Exception:
            continue
        if mapped.get("title") and mapped.get("job_url"):
            rows.append(mapped)
    return rows

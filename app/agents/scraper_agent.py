from datetime import date, timedelta
from scrapegraphai.graphs import SmartScraperGraph
from app.config import LLM_CONFIG
from app import db
from app.agents.finder_agent import find_careers_url
from app.agents.ats_detectors import detect_platform, fetch_with_method

JOB_SCHEMA_PROMPT = (
    "Extract every job posting listed on this careers/jobs page. "
    "For each job return: title, location, remote (true/false), salary (if shown), "
    "a short description, the direct job_url/link, and posted_date if shown "
    "(format YYYY-MM-DD, or null if unknown). "
    "Return a JSON list under the key 'jobs'."
)

GRAPH_CONFIG = {
    "llm": LLM_CONFIG,
    "verbose": False,
    "headless": True,
}


def _filter_recent(jobs: list[dict]) -> list[dict]:
    cutoff = date.today() - timedelta(days=30)
    recent = []
    for job in jobs:
        posted = job.get("posted_date")
        if posted:
            try:
                if date.fromisoformat(posted) < cutoff:
                    continue
            except ValueError:
                pass
        recent.append(job)
    return recent


def _save_result(company_id: int, jobs: list[dict], via: str):
    recent_jobs = _filter_recent(jobs)
    db.upsert_jobs(company_id, recent_jobs)
    db.mark_company_status(company_id, "done")
    db.log_scrape_run(company_id, "done", len(recent_jobs))
    return {"company_id": company_id, "status": "done", "jobs": len(recent_jobs), "via": via}


def scrape_company(company: dict) -> dict:
    """
    Scrape one company for recent job postings.
    1. If a cached deterministic method exists, use it (free, fast, no LLM).
    2. Otherwise, use the finder + AI scraper agent. If the discovered page
       matches a known ATS platform, cache a deterministic method for next time.
    """
    company_id = company["id"]

    method = db.get_method(company_id)
    if method:
        try:
            jobs = fetch_with_method(method["method_type"], method["method_config"])
            db.record_method_result(company_id, success=True)
            return _save_result(company_id, jobs, via=method["method_type"])
        except Exception as e:
            db.record_method_result(company_id, success=False)
            # fall through to AI path as a one-off retry

    url = company.get("careers_url")
    if not url:
        url = find_careers_url(company["name"], company.get("website"))
        if url:
            db.set_careers_url(company_id, url)
    if not url:
        return {"company_id": company_id, "status": "skipped", "jobs": 0, "error": "no url found"}

    method_type, method_config = detect_platform(url)
    if method_type:
        try:
            jobs = fetch_with_method(method_type, method_config)
            db.save_method(company_id, method_type, method_config)
            return _save_result(company_id, jobs, via=method_type)
        except Exception:
            pass  # fall back to AI scraping below

    try:
        graph = SmartScraperGraph(prompt=JOB_SCHEMA_PROMPT, source=url, config=GRAPH_CONFIG)
        result = graph.run()
        jobs = result.get("jobs", []) if isinstance(result, dict) else []
        return _save_result(company_id, jobs, via="ai")

    except Exception as e:
        db.mark_company_status(company_id, "error")
        db.log_scrape_run(company_id, "error", 0, error=str(e))
        return {"company_id": company_id, "status": "error", "jobs": 0, "error": str(e)}

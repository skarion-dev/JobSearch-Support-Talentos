"""
CEO agent: the conversational front door. Interprets natural-language
requests ("pull remote engineering jobs from fintech companies scraped
this week") and dispatches to the right tool: querying already-scraped
jobs, or kicking off scraping for a batch of companies.
"""
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from app.config import CEO_LLM_CONFIG, CEO_MODEL
from app import db
from app.agents.scraper_agent import scrape_company
from scripts.daily_scrape import run_aggregator_pass

SYSTEM_PROMPT = """You are the CEO agent for a job-search platform (Talentos).
You coordinate a job-scraping pipeline: an aggregator pass (bulk-pulls US jobs
from Adzuna and matches them to companies by name — this is the primary,
scalable path) and a per-company fallback pass (ATS-API detection or AI
scraping, used sparingly for companies the aggregator missed). You can:
- report stats on how many companies/jobs have been scraped
- query jobs already in the local database
- kick off an aggregator pass (fast, scales to any number of companies)
- kick off a small fallback batch for specific pending companies
Be concise and business-like. Always confirm scope before starting a large scrape."""


@tool
def get_stats() -> dict:
    """Return overall scraping stats: total companies, scraped count, total jobs, jobs posted in last 10 days."""
    return db.job_stats()


@tool
def list_recent_jobs(limit: int = 20) -> list[dict]:
    """List the most recently scraped jobs (title, company id, location, posted date)."""
    with db.get_conn() as conn:
        cur = conn.execute(
            """
            SELECT j.title, c.name, j.location, j.posted_date, j.job_url
            FROM jobs j JOIN companies c ON c.id = j.company_id
            ORDER BY j.scraped_at DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


@tool
def run_aggregator_batch(pages: int = 10) -> dict:
    """Run the Adzuna aggregator pass: bulk-pull recent US postings and match to companies. Scales to any company count."""
    companies_matched, jobs_found, _ = run_aggregator_pass(max_pages=pages)
    return {"companies_matched": companies_matched, "jobs_found": jobs_found}


@tool
def run_fallback_scrape_batch(batch_size: int = 20) -> dict:
    """Per-company ATS/AI scrape fallback for a small batch of pending companies. Not for large-scale use."""
    companies = db.fetch_companies(status="pending", limit=batch_size)
    results = [scrape_company(c) for c in companies]
    done = sum(1 for r in results if r["status"] == "done")
    errors = sum(1 for r in results if r["status"] == "error")
    total_jobs = sum(r.get("jobs", 0) for r in results)
    return {"attempted": len(results), "done": done, "errors": errors, "jobs_found": total_jobs}


def build_ceo_agent():
    llm = ChatOpenAI(
        model=CEO_MODEL,
        api_key=CEO_LLM_CONFIG["api_key"],
        base_url=CEO_LLM_CONFIG["base_url"],
    )
    tools = [get_stats, list_recent_jobs, run_aggregator_batch, run_fallback_scrape_batch]
    return create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)


def ask_ceo(agent, message: str, history: list):
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + history + [HumanMessage(content=message)]
    result = agent.invoke({"messages": messages})
    return result["messages"][-1].content

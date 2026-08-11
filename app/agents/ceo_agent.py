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

SYSTEM_PROMPT = """You are the CEO agent for a job-search platform (Talentos).
You coordinate a team of scraper agents that pull job postings from company
career pages. You can:
- report stats on how many companies/jobs have been scraped
- query jobs already in the local database
- kick off scraping runs for a batch of pending companies
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
def run_scrape_batch(batch_size: int = 20) -> dict:
    """Scrape a batch of pending companies (default 20) for recent job postings."""
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
    tools = [get_stats, list_recent_jobs, run_scrape_batch]
    return create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)


def ask_ceo(agent, message: str, history: list):
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + history + [HumanMessage(content=message)]
    result = agent.invoke({"messages": messages})
    return result["messages"][-1].content

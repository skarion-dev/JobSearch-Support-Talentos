# JobSearch Support - Talentos

Multi-agent AI system that scrapes company career pages for job postings from the
last 10 days, across the full Talentos company list (4,400+ companies).

## Architecture

Built to scale toward ~1M companies. Two scraping paths:

1. **Aggregator pass (primary)** — `app/agents/aggregators/adzuna.py` bulk-pulls recent US
   job postings from Adzuna and matches each posting's employer name against the companies
   table. A handful of API calls surfaces postings for many companies at once — this is the
   only approach that scales to millions of companies without per-company crawling.
2. **Fallback pass (secondary, small batches only)** — for companies the aggregator misses:
   - Deterministic ATS-API detectors (`app/agents/ats_detectors.py`): Greenhouse, Lever,
     Workday, SmartRecruiters, Ashby, Workable, Recruitee, BambooHR. Cached per company once
     detected, so repeat runs skip the LLM entirely.
   - AI scraper (`app/agents/scraper_agent.py`, via [ScrapeGraphAI](https://github.com/ScrapeGraphAI/Scrapegraph-ai))
     as a last resort for the long tail. Not reliable or fast enough for large-scale use.

- **CEO agent** (`app/agents/ceo_agent.py`) — conversational front door (Streamlit chat tab).
  Ask it for stats, recent jobs, or to kick off an aggregator/fallback pass. Runs on `deepseek-v4-pro`.
- **LLM**: [OpenCode Go](https://opencode.ai/go) subscription (OpenAI-compatible API at
  `https://opencode.ai/zen/go/v1`) — not the pay-per-credit Zen tier.
- **Local DB**: SQLite (`data/jobsearch.db`, gitignored) — companies, jobs, scrape_runs tables,
  see `db/schema.sqlite.sql`. No server/Docker required.
- **GUI**: Streamlit (`app/main.py`) — CEO Chat, Scrape Control (aggregator + fallback), Readiness, Jobs.
- **Hardcoded rules**: USA-only jobs (`app/filters.py`), 10-day posting retention window.

## Setup

```bash
cp .env.example .env
# fill in OPENCODE_API_KEY (and NEON_DB_URL if re-running the one-time import)

pip install -r requirements.txt
python -m playwright install chromium

python -m scripts.export_companies_from_neon   # one-time: seed companies table
streamlit run app/main.py
```

## Notes

- The Neon DB is only used as a one-time source for the company list — this repo's own
  DB is local SQLite, not connected to Talentos production at runtime.
- Job scraping is rate-limited by design; use the "Scrape Control" tab to run batches
  (default 20 companies) rather than the full list at once.
- `.env` and `data/*.db` are gitignored. Never commit real API keys or DB credentials.

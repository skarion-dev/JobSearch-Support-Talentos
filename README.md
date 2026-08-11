# JobSearch Support - Talentos

Multi-agent AI system that scrapes company career pages for job postings from the
last 10 days, across the full Talentos company list (4,400+ companies).

## Architecture

- **CEO agent** (`app/agents/ceo_agent.py`) — conversational front door (Streamlit chat tab).
  Ask it for stats, recent jobs, or to kick off a scrape batch. Runs on `deepseek-v4-pro`.
- **Scraper agent** (`app/agents/scraper_agent.py`) — uses [ScrapeGraphAI](https://github.com/ScrapeGraphAI/Scrapegraph-ai)
  to extract structured job data from each company's careers page. Runs on `deepseek-v4-flash`.
- **LLM**: [OpenCode Go](https://opencode.ai/go) subscription (OpenAI-compatible API at
  `https://opencode.ai/zen/go/v1`) — not the pay-per-credit Zen tier.
- **Local DB**: SQLite (`data/jobsearch.db`, gitignored) — companies, jobs, scrape_runs tables,
  see `db/schema.sqlite.sql`. No server/Docker required.
- **GUI**: Streamlit (`app/main.py`) — CEO Chat, manual Scrape Control, and a Jobs table.

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

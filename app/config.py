import os
from dotenv import load_dotenv

load_dotenv()

OPENCODE_API_KEY = os.getenv("OPENCODE_API_KEY", "")
# OpenCode Go subscription (not Zen pay-per-credit) uses the /zen/go/v1 path
OPENCODE_BASE_URL = os.getenv("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1")

# Scraper agents (bulk, cost-sensitive): deepseek-v4-flash
SCRAPER_MODEL = os.getenv("OPENCODE_SCRAPER_MODEL", "deepseek-v4-flash")
# CEO agent (orchestration/reasoning): deepseek-v4-pro
CEO_MODEL = os.getenv("OPENCODE_CEO_MODEL", "deepseek-v4-pro")

LOCAL_DB_PATH = os.getenv("LOCAL_DB_PATH", "data/jobsearch.db")
NEON_DB_URL = os.getenv("NEON_DB_URL", "")

LLM_CONFIG = {
    "api_key": OPENCODE_API_KEY,
    "model": f"openai/{SCRAPER_MODEL}",
    "base_url": OPENCODE_BASE_URL,
}

CEO_LLM_CONFIG = {
    "api_key": OPENCODE_API_KEY,
    "model": f"openai/{CEO_MODEL}",
    "base_url": OPENCODE_BASE_URL,
}

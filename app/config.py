import os
from dotenv import load_dotenv

load_dotenv()

# Raw OpenCode Go credentials — used directly only when the gateway isn't
# configured (see below). OpenCode Go subscription (not Zen pay-per-credit)
# uses the /zen/go/v1 path.
_RAW_OPENCODE_API_KEY = os.getenv("OPENCODE_API_KEY", "")
_RAW_OPENCODE_BASE_URL = os.getenv("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1")

# LLM gateway (gateway/main.py) — a proxy that pools OpenCode keys, enforces
# a 3-model allowlist, and rate-limits/logs every caller, including this app.
# When both are set, every agent below talks to the gateway instead of
# OpenCode directly, using a gateway-issued client token (see
# scripts/gateway_issue_key.py --name jobsearch-app), NOT the raw OpenCode
# key above. Left unset, everything falls back to calling OpenCode directly
# exactly as before — so a workstation without a gateway still works.
GATEWAY_URL = os.getenv("GATEWAY_URL", "")
GATEWAY_KEY = os.getenv("GATEWAY_KEY", "")

if GATEWAY_URL and GATEWAY_KEY:
    OPENCODE_API_KEY = GATEWAY_KEY
    OPENCODE_BASE_URL = GATEWAY_URL
else:
    OPENCODE_API_KEY = _RAW_OPENCODE_API_KEY
    OPENCODE_BASE_URL = _RAW_OPENCODE_BASE_URL

# Scraper agents (bulk, cost-sensitive): deepseek-v4-flash
SCRAPER_MODEL = os.getenv("OPENCODE_SCRAPER_MODEL", "deepseek-v4-flash")
# CEO agent (orchestration/reasoning): deepseek-v4-pro
CEO_MODEL = os.getenv("OPENCODE_CEO_MODEL", "deepseek-v4-pro")
# Keyword strategist runs once per night; a stronger model is affordable for a
# single high-leverage decision that allocates the whole day's API budget.
STRATEGIST_MODEL = os.getenv("OPENCODE_STRATEGIST_MODEL", "gpt-5.6-luna")

LOCAL_DB_PATH = os.getenv("LOCAL_DB_PATH", "data/jobsearch.db")
NEON_DB_URL = os.getenv("NEON_DB_URL", "")

ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")

# Ordered credential pool — the client rotates to the next account when one
# is exhausted (429) so a daily quota doesn't halt a run.
ADZUNA_ACCOUNTS = [
    (os.getenv(f"ADZUNA_APP_ID{sfx}", ""), os.getenv(f"ADZUNA_APP_KEY{sfx}", ""))
    for sfx in ("", "_2", "_3")
]
ADZUNA_ACCOUNTS = [(i, k) for i, k in ADZUNA_ACCOUNTS if i and k]

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

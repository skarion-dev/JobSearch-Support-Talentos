"""
Pull the Apify token pool from the Talentos Neon DB into the local .env.

Talentos keeps 45 tokens in job_agent_apify_tokens with a priority order and an
is_active flag, and rotates through them as each free-tier allowance is spent.
This copies the active ones locally so this build can use the same pool with
the same rotation behaviour.

READ-ONLY against Neon. Tokens are written to .env, which is gitignored, and
are never committed or logged in full.

Run: python -m scripts.sync_apify_tokens
"""
import os
import re

import psycopg

from app.config import NEON_DB_URL

ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")
QUERY = """
SELECT label, token_encrypted, priority
FROM job_agent_apify_tokens
WHERE is_active = true AND token_encrypted IS NOT NULL
ORDER BY priority
"""


def main(limit: int = 20):
    if not NEON_DB_URL:
        raise SystemExit("NEON_DB_URL not set")

    with psycopg.connect(NEON_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(QUERY)
            rows = cur.fetchall()

    tokens = []
    for _label, enc, _prio in rows:
        # Talentos stores these with a "bare:" prefix when not encrypted at rest
        tok = enc.split("bare:", 1)[1] if enc.startswith("bare:") else enc
        if tok.startswith("apify_api_"):
            tokens.append(tok)

    tokens = tokens[:limit]
    if not tokens:
        raise SystemExit("No usable Apify tokens found")

    with open(ENV_PATH, encoding="utf-8") as f:
        env = f.read()

    # drop any previously written pool
    env = re.sub(r"\n# --- Apify token pool.*?(?=\n[A-Z#]|\Z)", "", env, flags=re.S).rstrip()

    block = ["", "", "# --- Apify token pool (synced from Talentos job_agent_apify_tokens) ---",
             f"# {len(tokens)} tokens; the client rotates on quota/rate errors."]
    for i, tok in enumerate(tokens, 1):
        block.append(f"APIFY_TOKEN_{i}={tok}")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(env + "\n".join(block) + "\n")

    print(f"{len(rows)} active tokens in Neon -> wrote {len(tokens)} to .env as APIFY_TOKEN_1..{len(tokens)}")
    print(f"  sample: {tokens[0][:18]}...  (masked)")


if __name__ == "__main__":
    main()

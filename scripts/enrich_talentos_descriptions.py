"""
Fill in the truncated job descriptions already pushed to Talentos.

186 of the 431 jobs we pushed carry Adzuna snippets averaging 457 characters.
Adzuna truncates at ~500, so those rows were always going to be thin. The
resume generator and the AE both read this text, so it is worth repairing
before re-running any workflows.

Three passes, cheapest first:

  1. LOCAL      our own keyword_jobs already holds a longer version for many
                of them (measured: 49 rows, averaging 4,214 chars). Free.
  2. HTTP+LLM   fetch source_url and extract with deepseek-v4-flash. Works on
                employer sites and small boards; fails on LinkedIn/Indeed,
                which serve an anti-bot shell to a plain fetch.
  3. APIFY      LinkedIn/Indeed via their actors, which run real browsers.

Known dead ends, skipped rather than retried: links that resolve back to
adzuna.com (gated, needs a login) and a handful that the link finder resolved
to youtube.com and similar junk. Those need a better link, not a better
scraper.

Dry run by default.

    python -m scripts.enrich_talentos_descriptions
    python -m scripts.enrich_talentos_descriptions --commit
"""
import argparse
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg

from app import db
from app.config import NEON_DB_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("enrich")

MIN_GOOD = 1500          # what we consider a usable description
SOURCE_LABEL = "jobsearch_support"
DEAD_DOMAINS = ("adzuna.com", "youtube.com", "facebook.com", "twitter.com")


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fetch_thin(cur) -> list[dict]:
    cur.execute(
        """
        SELECT DISTINCT j.id, j.title, j.company, j.source_url, j.apply_url,
               length(coalesce(j.description_text, j.raw_description,'')) cur_len
        FROM applications a JOIN jobs j ON j.id = a.job_id
        WHERE a.source = %s
          AND length(coalesce(j.description_text, j.raw_description,'')) < %s
        """,
        (SOURCE_LABEL, MIN_GOOD),
    )
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def local_descriptions() -> dict[str, str]:
    """Best local description per normalized title|company."""
    out: dict[str, str] = {}
    with db.get_conn() as conn:
        for r in conn.execute(
            "SELECT title, company_name, description FROM keyword_jobs "
            "WHERE description IS NOT NULL"
        ).fetchall():
            key = f"{norm(r['title'])}|{norm(r['company_name'])}"
            d = r["description"] or ""
            if len(d) > len(out.get(key, "")):
                out[key] = d
    return out


def scrape_one(job: dict) -> tuple[str, str | None]:
    """HTTP + LLM extraction; returns (job_id, description or None)."""
    from app.agents.description_agent import extract
    url = job["source_url"] or job["apply_url"]
    if not url or any(d in url.lower() for d in DEAD_DOMAINS):
        return job["id"], None
    desc, _posted = extract(url)
    return job["id"], desc


def main(commit: bool, workers: int, limit: int | None):
    with psycopg.connect(NEON_DB_URL) as conn, conn.cursor() as cur:
        thin = fetch_thin(cur)
        if limit:
            thin = thin[:limit]
        log.info(f"{len(thin)} pushed jobs have a description under {MIN_GOOD} chars")

        # ---- pass 1: local copy (free) ----
        local = local_descriptions()
        updates: dict[str, tuple[str, str]] = {}     # job_id -> (text, via)
        for j in thin:
            cand = local.get(f"{norm(j['title'])}|{norm(j['company'])}")
            if cand and len(cand) > max(j["cur_len"], MIN_GOOD):
                updates[j["id"]] = (cand, "local")
        log.info(f"pass 1 local copy: {len(updates)} recoverable for free")

        # ---- pass 2: HTTP + LLM for the rest ----
        remaining = [j for j in thin if j["id"] not in updates]
        skipped_dead = [
            j for j in remaining
            if not (j["source_url"] or j["apply_url"])
            or any(d in (j["source_url"] or j["apply_url"] or "").lower() for d in DEAD_DOMAINS)
        ]
        scrapeable = [j for j in remaining if j not in skipped_dead]
        log.info(f"pass 2 scrape: {len(scrapeable)} candidates, {len(skipped_dead)} dead links skipped")

        if commit and scrapeable:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(scrape_one, j): j for j in scrapeable}
                for i, f in enumerate(as_completed(futs), 1):
                    jid, desc = f.result()
                    if desc and len(desc) >= MIN_GOOD:
                        updates[jid] = (desc, "scraped")
                    if i % 25 == 0:
                        log.info(f"  scraped {i}/{len(scrapeable)}, {len(updates)} total recovered")

        if not commit:
            log.info("DRY RUN - no writes. Pass 2 not executed.")
            log.info(f"would update at least {len(updates)} now, and attempt {len(scrapeable)} scrapes")
            return

        # ---- write back ----
        for jid, (text, _via) in updates.items():
            cur.execute(
                """UPDATE jobs
                   SET description_text = %s, raw_description = %s,
                       description_enriched_at = now(),
                       description_enrich_attempts = coalesce(description_enrich_attempts,0) + 1
                   WHERE id = %s""",
                (text, text, jid),
            )
        conn.commit()

        by_via = {}
        for _jid, (_t, via) in updates.items():
            by_via[via] = by_via.get(via, 0) + 1
        log.info(f"Updated {len(updates)} job descriptions in Talentos: {by_via}")
        log.info(f"Still thin: {len(thin) - len(updates)} (dead links or unscrapeable)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    main(a.commit, a.workers, a.limit)

"""7 PM Eastern TalentOS autopilot.

Only jobs ingested during this cycle are scored and pushed. Active base
resumes are synchronized from TalentOS at the start of every run, and every
new application is assigned to Akash and queued in the TalentOS AI pipeline.
"""
import argparse
import logging
import time
from datetime import datetime, timezone

from app import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("autopilot_cycle.log", encoding="utf-8")],
)
log = logging.getLogger("autopilot")


def watermark_utc() -> str:
    # SQLite stores scraped_at in UTC-like YYYY-MM-DD HH:MM:SS form.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def main(dry_run: bool = False, skip_ingest: bool = False) -> int:
    started = time.time()
    log.info("########## 7PM AUTOPILOT START ##########")

    from scripts.daily_cycle import (
        s1_sync, s2_keywords, s3_ingest, s3b_dedicated_ingest,
        WINDOW_DAYS,
    )
    from scripts.match_resumes_to_jobs import main as match
    from scripts.push_to_talentos import load_matches, push

    # Sync first: this is the source of truth for active candidates/resumes and
    # carries every manager-edited rule into the local matcher.
    active = s1_sync()
    if not active:
        log.warning("No active base resumes were returned; refusing to ingest or push")
        return 2

    since = watermark_utc()
    log.info("New-job watermark: %s UTC", since)

    if not skip_ingest:
        keywords = s2_keywords(500) or []
        if keywords:
            s3_ingest(keywords, apify=True, adzuna=True)
            s3b_dedicated_ingest()
        else:
            log.warning("No keywords selected; refusing to match or push")
            return 3

    # Do not enrich historical matches here. The nightly contract is strictly
    # new-ingestion-only; thin new postings are held by push quality gates.
    run_id = match(
        top_n=50,
        days=1,
        workers=4,
        include_test=False,
        skip_done=False,
        pool_size=250,
        posted_days=WINDOW_DAYS,
        skip_existing=True,
        since=since,
    )
    if not run_id:
        log.info("No matcher run was created; nothing to push")
        return 0

    matches = load_matches(
        limit=0,
        min_score=0,
        posted_days=3650,
        per_candidate_cap=10000,
        run_id=run_id,
    )
    log.info("Run %s produced %d candidate/job pairs", run_id, len(matches))
    if dry_run:
        log.info("DRY RUN: TalentOS was not modified")
        return 0

    stats, _plan = push(
        matches,
        commit=True,
        actor="JobSearch Autopilot 7PM",
        stagger_seconds=0.5,
    )
    log.info("Autopilot push stats: %s", dict(stats))
    log.info("########## AUTOPILOT DONE in %.1f min ##########", (time.time() - started) / 60)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-ingest", action="store_true")
    args = ap.parse_args()
    raise SystemExit(main(dry_run=args.dry_run, skip_ingest=args.skip_ingest))

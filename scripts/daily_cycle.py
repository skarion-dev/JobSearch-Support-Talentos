"""
Nightly cycle — runs at 00:00 and prepares the day's shortlist for review.

Six stages, in order. Each is independently re-runnable and a failure in one
does not corrupt the others; the run is recorded either way.

  1 sync      pull active candidates + approved base resumes from Talentos
  2 keywords  gpt-5.6-luna picks tonight's search terms from measured ROI
  3 ingest    Adzuna + Apify (LinkedIn/Indeed/Google), 24h window
  4 enrich    recover readable links, pull full descriptions
  5 match     deepseek-v4-flash scores jobs per base resume, 100-wide
  6 report    write run stats

This NEVER writes to Talentos. A human reviews in the UI and clicks Assign.

Run: python -m scripts.daily_cycle
     python -m scripts.daily_cycle --skip-ingest      (re-match only)
"""
import argparse
import csv
import logging
import os
import time
import traceback

from app import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("daily_cycle.log", encoding="utf-8")],
)
log = logging.getLogger("daily_cycle")

WINDOW_DAYS = 1          # postings from the last 24 hours only
ROI_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "roi_keywords.csv")


def stage(name):
    """Run a stage, log timing, never let one failure kill the cycle."""
    def deco(fn):
        def wrapped(*a, **kw):
            log.info(f"=== STAGE: {name} ===")
            t0 = time.time()
            try:
                out = fn(*a, **kw)
                log.info(f"--- {name} ok in {time.time()-t0:.0f}s")
                return out
            except Exception as e:
                log.error(f"--- {name} FAILED after {time.time()-t0:.0f}s: {e}")
                log.debug(traceback.format_exc())
                return None
        return wrapped
    return deco


@stage("1 sync profiles")
def s1_sync():
    from scripts.sync_resume_profiles import main as sync
    sync()
    with db.get_conn() as conn:
        n = conn.execute("SELECT count(*) FROM resume_profiles WHERE is_test_account=0").fetchone()[0]
    log.info(f"{n} active base-resume profiles")
    return n


@stage("2 choose keywords")
def s2_keywords(n_keywords: int):
    from app.agents.keyword_strategist import choose_keywords
    keywords, reasoning = choose_keywords(n=n_keywords)
    log.info(f"strategist chose {len(keywords)} keywords")
    log.info(f"rationale: {reasoning}")
    with open(ROI_PATH.replace("roi_keywords", "tonight_keywords"), "w",
              encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["keyword"])
        w.writerows([[k] for k in keywords])
    return keywords


@stage("3 ingest")
def s3_ingest(keywords: list[str], apify: bool):
    before = _job_count()

    from scripts.keyword_search import main as adzuna_search
    adzuna_search(top_n=len(keywords), days=WINDOW_DAYS, workers=20,
                  max_pages=3, call_budget=450, source="roi")

    if apify:
        from scripts.apify_ingest import main as apify_ingest
        for src, chunk in (("linkedin", 50), ("indeed", 1), ("google", 6)):
            try:
                apify_ingest(src, top=min(len(keywords), 120 if src != "indeed" else 40),
                             max_items=500, days=WINDOW_DAYS, chunk=chunk)
            except Exception as e:
                log.warning(f"apify {src} failed: {e}")

    added = _job_count() - before
    log.info(f"ingested {added} new jobs")
    return added


@stage("4 enrich")
def s4_enrich():
    from scripts.backfill_source_links import main as links
    from scripts.backfill_descriptions import main as descs
    links(limit=None, workers=6, matched_only=True)
    descs(limit=None, workers=60, matched_only=True)


@stage("5 match")
def s5_match(workers: int):
    from scripts.match_resumes_to_jobs import main as match
    match(top_n=50, days=2, workers=workers, include_test=False,
          skip_done=False, pool_size=250, posted_days=WINDOW_DAYS)


@stage("6 report")
def s6_report():
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT p.candidate_name, count(*) n,
                   sum(CASE WHEN m.band='TOP_MATCH' THEN 1 ELSE 0 END) tops
            FROM resume_job_matches m JOIN resume_profiles p ON p.id=m.resume_profile_id
            GROUP BY p.candidate_name ORDER BY n DESC
        """).fetchall()
    for r in rows:
        log.info(f"  {r['n']:>4} matches ({r['tops']} top)  {r['candidate_name']}")
    total = sum(r["n"] for r in rows)
    log.info(f"READY FOR REVIEW: {total} matches across {len(rows)} candidates")
    return total


def _job_count() -> int:
    with db.get_conn() as conn:
        return conn.execute("SELECT count(*) FROM keyword_jobs").fetchone()[0]


def main(n_keywords: int, workers: int, skip_ingest: bool, apify: bool):
    t0 = time.time()
    log.info("########## NIGHTLY CYCLE START ##########")

    db.purge_old_jobs(days=10)
    s1_sync()
    keywords = s2_keywords(n_keywords) or []
    if not skip_ingest and keywords:
        s3_ingest(keywords, apify)
    s4_enrich()
    s5_match(workers)
    s6_report()

    log.info(f"########## CYCLE DONE in {(time.time()-t0)/60:.1f} min ##########")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keywords", type=int, default=500)
    ap.add_argument("--workers", type=int, default=100)
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--no-apify", action="store_true")
    a = ap.parse_args()
    main(a.keywords, a.workers, a.skip_ingest, apify=not a.no_apify)

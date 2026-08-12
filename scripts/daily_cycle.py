"""
Nightly cycle — runs at 00:00 and prepares the day's shortlist for review.

Seven stages, in order. Each is independently re-runnable and a failure in one
does not corrupt the others; the run is recorded either way.

  1 sync      pull active candidates + approved base resumes from Talentos
  2 keywords  gpt-5.6-luna picks tonight's search terms from measured ROI
  3 ingest    Apify first (full descriptions), Adzuna second (discovery)
  4 enrich    recover readable links, pull full descriptions
  5 match     deepseek-v4-flash scores jobs per base resume, 100-wide
  6 export    write the manual-chase sheet for what cannot be automated
  7 report    per-source yield and what is ready for review

This NEVER writes to Talentos. A human reviews in the UI and clicks Assign.

WHY APIFY LEADS
---------------
Measured over 28,325 ingested jobs:

    adzuna           25,938 jobs     224 usable    (1%)   avg   535 chars
    apify:linkedin    2,150 jobs   2,093 usable   (97%)   avg 5,473 chars
    apify:indeed        227 jobs     225 usable   (99%)   avg 5,622 chars

Adzuna was 92% of the corpus and 9% of what could actually be pushed, because
it truncates at ~500 characters and gates its own links behind a login — there
is nothing left to scrape. Roughly half of every candidate's matches were being
skipped at push time as a result.

So Apify now runs first and wide, and Adzuna's budget is cut to what earns its
keep. Adzuna is kept rather than dropped: it still surfaced 80 usable matches
and covers boards the actors do not reach. It is a discovery signal, not a
source of applications.

Run: python -m scripts.daily_cycle
     python -m scripts.daily_cycle --skip-ingest      (re-match only)
     python -m scripts.daily_cycle --no-adzuna        (Apify only)
"""
import argparse
import csv
import logging
import os
import time
import traceback
from datetime import datetime

from app import db
from app.quality import MIN_DESCRIPTION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("daily_cycle.log", encoding="utf-8")],
)
log = logging.getLogger("daily_cycle")

WINDOW_DAYS = 1          # postings from the last 24 hours only
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
ROI_PATH = os.path.join(DATA_DIR, "roi_keywords.csv")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")

# Apify plan, per source. chunk is a hard constraint, not a tuning knob:
# Indeed's query parser returns nothing for OR-joined lists so it must be driven
# one keyword per run, while LinkedIn and Google accept a batch.
APIFY_PLAN = [
    #  source      keywords  max_items  chunk
    ("linkedin",   400,      1000,      50),
    ("indeed",     120,      400,       1),
    ("google",     60,       400,       6),
]


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

    tonight_csv = ROI_PATH.replace("roi_keywords", "tonight_keywords")
    with open(tonight_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["keyword"])
        w.writerows([[k] for k in keywords])

    # Luna's rationale had nowhere to live but the log file -- the Keywords
    # tab reads this to show *why* tonight's set was chosen, not just what.
    import json
    with open(tonight_csv.replace(".csv", ".json"), "w", encoding="utf-8") as f:
        json.dump({
            "chosen_at": datetime.now().isoformat(timespec="seconds"),
            "n_requested": n_keywords,
            "n_chosen": len(keywords),
            "reasoning": reasoning,
        }, f, indent=2)

    return keywords


@stage("3 ingest")
def s3_ingest(keywords: list[str], apify: bool, adzuna: bool):
    before = _job_count()

    # Apify first and widest — these are the jobs that can actually be pushed.
    if apify:
        from scripts.apify_ingest import main as apify_ingest
        for src, top, max_items, chunk in APIFY_PLAN:
            try:
                apify_ingest(src, top=min(len(keywords), top),
                             max_items=max_items, days=WINDOW_DAYS, chunk=chunk)
            except Exception as e:
                log.warning(f"apify {src} failed: {e}")

    # Adzuna second, on a smaller budget. Broad coverage of boards the actors
    # miss, but 1% of it survives the description gate.
    if adzuna:
        from scripts.keyword_search import main as adzuna_search
        adzuna_search(top_n=len(keywords), days=WINDOW_DAYS, workers=20,
                      max_pages=2, call_budget=250, source="roi")

    added = _job_count() - before
    log.info(f"ingested {added} new jobs")
    _log_source_yield()
    return added


def _log_source_yield():
    """Per-source usable rate for tonight's intake — the number that matters."""
    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT source, count(*) n,
                   sum(CASE WHEN length(coalesce(description,'')) >= {MIN_DESCRIPTION}
                            THEN 1 ELSE 0 END) usable
            FROM keyword_jobs
            WHERE date(scraped_at) = date('now')
            GROUP BY source ORDER BY n DESC
        """).fetchall()
    for r in rows:
        pct = 100 * r["usable"] / r["n"] if r["n"] else 0
        log.info(f"  {str(r['source']):<16} {r['n']:>6} jobs  {r['usable']:>5} usable ({pct:.0f}%)")


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


@stage("6 export manual-chase sheet")
def s6_export():
    """
    Whatever matched well but cannot be automated becomes a worklist, not a
    silent drop. The same workbook is downloadable in the UI; this copy means
    it also exists on disk without anyone having to open the app.
    """
    from app import exports
    rows = exports.unpursued_rows(min_score=90)
    if not rows:
        log.info("nothing needs manual chasing")
        return 0

    path = exports.archive_path(EXPORT_DIR)
    with open(path, "wb") as f:
        f.write(exports.build_workbook(rows))

    summary = exports.summarise(rows)
    log.info(f"{summary['total']} jobs need manual chasing -> {os.path.abspath(path)}")
    for why, n in sorted(summary["by_reason"].items(), key=lambda x: -x[1]):
        log.info(f"    {n:>4}  {why}")
    return summary["total"]


@stage("7 report")
def s7_report():
    """
    'sendable' means net-new: matched, adequately described, AND not already
    logged in Talentos for that candidate by any source. Without the last
    check this number double-counts jobs the previous night already sent —
    the push itself would silently skip them, but the log would keep claiming
    them as fresh opportunity every night until they aged out of the window.
    """
    from app.talentos_state import fetch_logged_state, is_logged

    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT p.candidate_id, p.candidate_name, m.band,
                   j.title, j.company_name, j.external_job_id,
                   j.source_url, j.apply_url, j.job_url,
                   length(coalesce(j.description,'')) desc_len
            FROM resume_job_matches m
            JOIN resume_profiles p ON p.id = m.resume_profile_id
            JOIN keyword_jobs   j ON j.id = m.keyword_job_id
            WHERE p.is_test_account = 0
        """).fetchall()]

    cand_ids = sorted({str(r["candidate_id"]) for r in rows})
    state = fetch_logged_state(cand_ids) if cand_ids else {
        "external": set(), "url": set(), "title": set(), "title_global": {}}

    agg: dict[str, dict] = {}
    already_logged = 0
    for r in rows:
        if is_logged(r["candidate_id"], state, external_job_id=r["external_job_id"],
                    apply_url=r["apply_url"], source_url=r["source_url"],
                    job_url=r["job_url"], company=r["company_name"], title=r["title"]):
            already_logged += 1
            continue
        a = agg.setdefault(r["candidate_name"], {"n": 0, "tops": 0, "ready": 0})
        a["n"] += 1
        if r["band"] == "TOP_MATCH":
            a["tops"] += 1
        if r["desc_len"] >= MIN_DESCRIPTION:
            a["ready"] += 1

    for name, a in sorted(agg.items(), key=lambda kv: -kv[1]["ready"]):
        log.info(f"  {a['ready']:>4} sendable / {a['n']:>4} net-new "
                 f"({a['tops']} top)  {name}")

    total = sum(a["n"] for a in agg.values())
    ready = sum(a["ready"] for a in agg.values())
    log.info(f"READY TO SEND: {ready} of {total} net-new matches across {len(agg)} candidates "
             f"({already_logged} already in Talentos, excluded)")
    if total:
        log.info(f"automatable rate: {100*ready/total:.0f}% "
                 f"({total - ready} for manual chase)")
    return ready


def _job_count() -> int:
    with db.get_conn() as conn:
        return conn.execute("SELECT count(*) FROM keyword_jobs").fetchone()[0]


def main(n_keywords: int, workers: int, skip_ingest: bool,
         apify: bool = True, adzuna: bool = True):
    t0 = time.time()
    log.info("########## NIGHTLY CYCLE START ##########")

    db.purge_old_jobs(days=10)
    s1_sync()
    keywords = s2_keywords(n_keywords) or []
    if not skip_ingest and keywords:
        s3_ingest(keywords, apify, adzuna)
    s4_enrich()
    s5_match(workers)
    s6_export()
    s7_report()

    log.info(f"########## CYCLE DONE in {(time.time()-t0)/60:.1f} min ##########")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keywords", type=int, default=500)
    ap.add_argument("--workers", type=int, default=100)
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--no-apify", action="store_true")
    ap.add_argument("--no-adzuna", action="store_true",
                    help="Apify only — highest quality, lowest volume")
    a = ap.parse_args()
    main(a.keywords, a.workers, a.skip_ingest,
         apify=not a.no_apify, adzuna=not a.no_adzuna)
